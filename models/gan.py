""" This module implement a gan task """
# __Author__ == "Haowen Xu"
# __Data__ == "04-29-2018"

import tensorflow as tf
import tensorflow.contrib.slim as slim
import numpy as np
import math
import os

from dataio.dataset_cifar10 import Dataset_cifar10
from dataio.dataset_mnist import Dataset_mnist
import utils
from utils import inception_score_mnist
from utils import save_images
from models import layers
from models.basic_model import Basic_model
logger = utils.get_logger()

class Gan(Basic_model):
    def __init__(self, config, exp_name='new_exp_gan'):
        self.config = config
        self.graph = tf.Graph()
        self.exp_name = exp_name
        gpu_options = tf.GPUOptions(allow_growth=True)
        configProto = tf.ConfigProto(gpu_options=gpu_options)
        self.sess = tf.InteractiveSession(config=configProto,
                                            graph=self.graph)
        if config.gan_mode == 'dcgan_mnist':
            self.train_dataset = Dataset_mnist()
            self.train_dataset.load_mnist(config.data_dir,
                                          tf.estimator.ModeKeys.TRAIN)
            self.valid_dataset = Dataset_mnist()
            self.valid_dataset.load_mnist(config.data_dir,
                                          tf.estimator.ModeKeys.EVAL)
        elif config.gan_mode == 'dcgan':
            self.train_dataset = Dataset_cifar10()
            self.train_dataset.load_cifar10(config.data_dir,
                                            tf.estimator.ModeKeys.TRAIN)
            self.valid_dataset = Dataset_cifar10()
            self.valid_dataset.load_cifar10(config.data_dir,
                                            tf.estimator.ModeKeys.EVAL)
        else:
            raise NotImplementedError('Invalid gan_mode')

        self._build_placeholder()
        self._build_graph()
        self.reward_baseline = None # average reward over episodes
        self.reset()
        self.fixed_noise_128 = np.random.normal(size=(128, config.dim_z))\
            .astype('float32')

    def reset(self):
        # ----Reset the model.----
        # TODO(haowen) The way to carry step number information should be
        # reconsiderd
        self.step_number = 0
        self.ema_gen_cost = None
        self.ema_disc_cost_real = None
        self.ema_disc_cost_fake = None
        self.prst_gen_cost = None
        self.prst_disc_cost_real = None
        self.prst_disc_cost_fake = None
        self.mag_gen_grad = None
        self.mag_disc_grad = None
        self.inception_score = 0
        self.previous_action = -1
        self.same_action_count = 0
        self.task_dir = None

        # to control when to terminate the episode
        self.endurance = 0
        self.best_inception_score = 0
        self.inps_baseline = 0
        self.collapse = False

    def _build_placeholder(self):
        with self.graph.as_default():
            dim_x = self.config.dim_x
            dim_z = self.config.dim_z
            self.real_data = tf.placeholder(tf.float32, shape=[None, dim_x],
                                                name='real_data')
            self.noise = tf.placeholder(tf.float32, shape=[None, dim_z],
                                        name='noise')
            self.is_training = tf.placeholder(tf.bool, name='is_training')

    def _build_graph(self):
        dim_x = self.config.dim_x
        dim_z = self.config.dim_z
        batch_size = self.config.batch_size
        lr = self.config.lr_stud
        beta1 = self.config.beta1
        beta2 = self.config.beta2

        with self.graph.as_default():
            real_data = tf.cast(self.real_data, tf.float32)
            fake_data = self.generator(self.noise)
            disc_real = self.discriminator(real_data)
            disc_fake = self.discriminator(fake_data, reuse=True)

            if self.config.gan_mode == 'dcgan' or\
                    self.config.gan_mode == 'dcgan_mnist':
                gen_cost = tf.reduce_mean(
                    tf.nn.sigmoid_cross_entropy_with_logits(
                        logits=disc_fake, labels=tf.ones_like(disc_fake)
                    )
                )
                disc_cost_fake = tf.reduce_mean(
                    tf.nn.sigmoid_cross_entropy_with_logits(
                        logits=disc_fake, labels=tf.zeros_like(disc_fake)
                    )
                )
                disc_cost_real = tf.reduce_mean(
                    tf.nn.sigmoid_cross_entropy_with_logits(
                        logits=disc_real, labels=tf.ones_like(disc_real)
                    )
                )
                disc_cost = (disc_cost_fake + disc_cost_real) / 2.

                tvars = tf.trainable_variables()
                gen_tvars = [v for v in tvars if 'Generator' in v.name]
                disc_tvars = [v for v in tvars if 'Discriminator' in v.name]

                gen_grad = tf.gradients(gen_cost, gen_tvars)
                disc_grad = tf.gradients(disc_cost, disc_tvars)
                optimizer = tf.train.AdamOptimizer(learning_rate=lr,
                                                   beta1=beta1,
                                                   beta2=beta2)
                gen_train_op = optimizer.apply_gradients(
                    zip(gen_grad, gen_tvars))
                disc_train_op = optimizer.apply_gradients(
                    zip(disc_grad, disc_tvars))
            else:
                raise Exception('Invalid gan_mode')

            self.saver = tf.train.Saver()
            self.init = tf.global_variables_initializer()
            self.fake_data = fake_data
            self.gen_train_op = gen_train_op
            self.disc_train_op = disc_train_op
            self.update = [gen_train_op, disc_train_op]

            self.gen_cost = gen_cost
            self.gen_grad = gen_grad
            self.gen_tvars = gen_tvars

            self.disc_cost_fake = disc_cost_fake
            self.disc_cost_real = disc_cost_real
            self.disc_grad = disc_grad
            self.disc_tvars = disc_tvars

            self.grads = gen_grad + disc_grad

    def generator(self, input):
        dim_z = self.config.dim_z
        dim_c = self.config.dim_c
        with tf.variable_scope('Generator'):
            output = layers.linear(input, 7*7*2*dim_c, name='LN1')
            #output = layers.batchnorm(output, is_training=self.is_training,
            #                          name='BN1')
            output = tf.nn.relu(output)
            output = tf.reshape(output, [-1, 7, 7, 2*dim_c])

            output_shape = [-1, 14, 14, dim_c]
            output = layers.deconv2d(output, output_shape, name='Deconv2')
            #output = layers.batchnorm(output, is_training=self.is_training,
            #                          name='BN2')
            output = tf.nn.relu(output)

            output_shape = [-1, 28, 28, 1]
            output = layers.deconv2d(output, output_shape, name='Decovn3')
            output = tf.nn.tanh(output)

            return tf.reshape(output, [-1, 28*28])

    def discriminator(self, inputs, reuse=False):
        dim_c = self.config.dim_c
        with tf.variable_scope('Discriminator') as scope:
            if reuse:
                scope.reuse_variables()

            output = tf.reshape(inputs, [-1, 28, 28, 1])

            output = layers.conv2d(output, dim_c, name='Conv1')
            output = tf.nn.leaky_relu(output)

            output = layers.conv2d(output, 2*dim_c, name='Conv2')
            #output = layers.batchnorm(output, is_training=self.is_training,
            #                          name='BN2')
            output = tf.nn.leaky_relu(output)

            output = tf.reshape(output, [-1, 7*7*2*dim_c])
            output = layers.linear(output, 1, name='LN3')

            return tf.reshape(output, [-1])

    def train(self, save_model=False):
        sess = self.sess
        config = self.config
        batch_size = config.batch_size
        dim_z = config.dim_z
        valid_frequency = config.valid_frequency_stud
        print_frequency = config.print_frequency_stud
        max_endurance = config.max_endurance_stud
        endurance = 0
        best_inps = 0
        inps_baseline = 0
        decay = config.metric_decay

        for step in range(config.max_training_step):
            # ----Update D network.----
            for i in range(config.disc_iters):
                data = self.train_dataset.next_batch(batch_size)
                x = data['input']

                z = np.random.normal(size=[batch_size, dim_z]).astype(np.float32)
                feed_dict = {self.noise: z, self.real_data: x,
                             self.is_training: True}
                sess.run(self.disc_train_op, feed_dict=feed_dict)

            # ----Update G network.----
            for i in range(config.gen_iters):
                z = np.random.normal(size=[batch_size, dim_z]).astype(np.float32)
                feed_dict = {self.noise: z, self.is_training: True}
                sess.run(self.gen_train_op, feed_dict=feed_dict)

            if step % valid_frequency == (valid_frequency - 1):
                logger.info('========Step{}========'.format(step + 1))
                logger.info(endurance)
                inception_score = self.get_inception_score(config.inps_batches)
                logger.info(inception_score)
                if inps_baseline > 0:
                    inps_baseline = inps_baseline * decay \
                        + inception_score[0] * (1 - decay)
                else:
                    inps_baseline = inception_score[0]
                logger.info('inps_baseline: {}'.format(inps_baseline))
                self.generate_images(step)
                endurance += 1
                if inps_baseline > best_inps:
                    best_inps = inps_baseline
                    endurance = 0
                    if save_model:
                        self.save_model(step)

            if step % print_frequency == (print_frequency - 1):
                data = self.train_dataset.next_batch(batch_size)
                x = data['input']
                z = np.random.normal(size=[batch_size, dim_z]).astype(np.float32)
                feed_dict = {self.noise: z, self.real_data: x,
                             self.is_training: False}
                fetch = [self.gen_cost,
                         self.disc_cost_fake,
                         self.disc_cost_real]
                r = sess.run(fetch, feed_dict=feed_dict)
                logger.info('gen_cost: {}'.format(r[0]))
                logger.info('disc_cost fake: {}, real: {}'.format(r[1], r[2]))

            if endurance > max_endurance:
                break
        logger.info(best_inps)

    def response(self, action):
        # Given an action, return the new state, reward and whether dead

        # Args:
        #     action: one hot encoding of actions

        # Returns:
        #     state: shape = [dim_state_rl]
        #     reward: shape = [1]
        #     dead: boolean
        #
        sess = self.sess
        batch_size = self.config.batch_size
        dim_z = self.config.dim_z
        alpha = self.config.state_decay

        data = self.train_dataset.next_batch(batch_size)
        x = data['input']
        z = np.random.normal(size=[batch_size, dim_z]).astype(np.float32)
        feed_dict = {self.noise: z, self.real_data: x,
                     self.is_training: True}
        a = np.argmax(np.array(action))

        # ----To detect collapse.----
        if a == self.previous_action:
            self.same_action_count += 1
        else:
            self.same_action_count = 0
        self.previous_action = a

        fetch = [self.update[a], self.gen_grad, self.disc_grad,
                 self.gen_cost, self.disc_cost_real, self.disc_cost_fake]
        _, gen_grad, disc_grad, gen_cost, disc_cost_real, disc_cost_fake = \
            sess.run(fetch, feed_dict=feed_dict)

        self.mag_gen_grad = self.get_grads_magnitude(gen_grad)
        self.mag_disc_grad = self.get_grads_magnitude(disc_grad)
        self.prst_gen_cost = gen_cost
        self.prst_disc_cost_real = disc_cost_real
        self.prst_disc_cost_fake = disc_cost_fake

        # ----Update state.----
        self.step_number += 1
        if self.ema_gen_cost is None:
            self.ema_gen_cost = gen_cost
            self.ema_disc_cost_real = disc_cost_real
            self.ema_disc_cost_fake = disc_cost_fake
        else:
            self.ema_gen_cost = self.ema_gen_cost * alpha\
                + gen_cost * (1 - alpha)
            self.ema_disc_cost_real = self.ema_disc_cost_real * alpha\
                + disc_cost_real * (1 - alpha)
            self.ema_disc_cost_fake = self.ema_disc_cost_fake * alpha\
                + disc_cost_fake * (1 - alpha)


        reward = self.get_step_reward()
        # ----Early stop and record best result.----
        dead = self.check_terminate()
        state = self.get_state()
        return state, reward, dead

    def update_inception_score(self, score):
        self.best_inception_score = score

    def get_state(self):
        if self.step_number == 0:
            state = [0] * self.config.dim_state_rl
        else:
            state = [self.step_number / self.config.max_training_step,
                     math.log(self.mag_disc_grad / self.mag_gen_grad),
                     self.ema_gen_cost,
                     self.ema_disc_cost_real,
                     self.ema_disc_cost_fake,
                     self.inception_score / 10,
                     ]
        return np.array(state, dtype='f')

    def check_terminate(self):
        # TODO(haowen)
        # Early stop and recording the best result
        # Episode terminates on two condition:
        # 1) Convergence: inception score doesn't improve in endurance steps
        # 2) Collapse: action space collapse to one action
        if self.same_action_count > 500:
            logger.info('Terminate reason: Collapse')
            self.collapse = True
            return True
        step = self.step_number
        if step % self.config.valid_frequency_stud == 0:
            self.endurance += 1
            inception_score = self.get_inception_score(self.config.inps_batches)
            inps = inception_score[0]
            self.inception_score = inps
            decay = self.config.metric_decay
            if self.inps_baseline > 0:
                self.inps_baseline = self.inps_baseline * decay \
                                     + inps * (1 - decay)
            else:
                self.inps_baseline = inps
            if self.inps_baseline > self.best_inception_score:
                logger.info('step: {}, new best result: {}'.\
                            format(step, self.inps_baseline))
                self.best_step = self.step_number
                self.best_inception_score = self.inps_baseline
                self.endurance = 0
                self.save_model(step)

        if step % self.config.print_frequency_stud == 0:
            logger.info('----step{}----'.format(step))
            logger.info('inception_score: {}'.format(inception_score))
            logger.info('inps_baseline: {}'.format(self.inps_baseline))

        if step > self.config.max_training_step:
            return True

        if self.config.stop_strategy_stud == 'prescribed_steps':
            pass
        elif self.config.stop_strategy_stud == 'exceeding_endurance' and \
                self.endurance > self.config.max_endurance_stud:
            return True
        elif self.config.stop_strategy_stud == 'prescribed_inps':
            if self.inps_baseline > self.config.inps_threshold:
                return True
        return False

    def get_step_reward(self):
        return 0

    def get_final_reward(self):
        if self.collapse:
            return 0, -self.config.reward_max_value
        if self.config.stop_strategy_stud == 'prescribed_steps' or \
                self.config.stop_strategy_stud == 'exceeding_endurance':
            inps = self.best_inception_score
            reward = self.config.reward_c * inps ** 2
        elif self.config.stop_strategy_stud == 'prescribed_inps':
            time_cost = self.step_number / self.config.max_training_step
            reward = math.sqrt(self.config.reward_c / time_cost)
            if self.step_number > self.config.max_training_step:
                return 0, -self.config.reward_max_value

        if self.reward_baseline is None:
            self.reward_baseline = reward
        decay = self.config.reward_baseline_decay
        adv = reward - self.reward_baseline
        adv = min(adv, self.config.reward_max_value)
        adv = max(adv, -self.config.reward_max_value)
        # ----Shift average----
        self.reward_baseline = decay * self.reward_baseline\
            + (1 - decay) * reward
        return reward, adv

    def get_inception_score(self, num_batches):
        all_samples = []
        config = self.config
        batch_size = 100
        dim_z = config.dim_z
        for i in range(num_batches):
            z = np.random.normal(size=[batch_size, dim_z]).astype(np.float32)
            feed_dict = {self.noise: z, self.is_training: False}
            samples = self.sess.run(self.fake_data, feed_dict=feed_dict)
            all_samples.append(samples)
        all_samples = np.concatenate(all_samples, axis=0)
        all_samples = all_samples.reshape((-1, 28*28))
        return inception_score_mnist.get_inception_score(all_samples,
                                                   splits=config.inps_splits)

    def generate_images(self, step):
        feed_dict = {self.noise: self.fixed_noise_128,
                     self.is_training: False}
        samples = self.sess.run(self.fake_data, feed_dict=feed_dict)
        #samples = ((samples+1.)*255./2.).astype('int32')
        task_dir = os.path.join(self.config.save_images_dir, self.exp_name)
        if not os.path.exists(task_dir):
            os.mkdir(task_dir)
        save_path = os.path.join(task_dir, 'images_{}.jpg'.format(step))
        save_images.save_images(samples.reshape((-1, 28, 28, 1)), save_path)


if __name__ == '__main__':
    root_path = os.path.dirname(os.path.realpath(__file__))
    config_path = os.path.join(root_path, 'config/' + 'gan.cfg')
    config = utils.Parser(config_path)
    gan = Gan(config)
