""" This module implement a gan task """
# __Author__ == "Haowen Xu"
# __Data__ == "04-29-2018"

import tensorflow as tf
import tensorflow.contrib.slim as slim
import numpy as np
import math
import time
import os

from dataio.dataset_cifar10 import Dataset_cifar10
import utils
from utils import inception_score
from utils import save_images
from models import layers
from models.basic_model import Basic_model

logger = utils.get_logger()

class Gan(Basic_model):
    def __init__(self, config, exp_name='new_exp'):
        self.config = config
        self.graph = tf.Graph()
        self.exp_name = exp_name
        gpu_options = tf.GPUOptions(allow_growth=True)
        configProto = tf.ConfigProto(gpu_options=gpu_options)
        self.sess = tf.InteractiveSession(config=configProto,
                                            graph=self.graph)
        # ----Loss_mode is only for DEBUG usage.----
        self.train_dataset = Dataset_cifar10()
        self.train_dataset.load_cifar10(config.data_dir,
                                        tf.estimator.ModeKeys.TRAIN)
        self.valid_dataset = Dataset_cifar10()
        self.valid_dataset.load_cifar10(config.data_dir,
                                        tf.estimator.ModeKeys.EVAL)
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
        self.inception_score = 0

        # to control when to terminate the episode
        self.endurance = 0
        self.best_inception_score = 0

    def _build_placeholder(self):
        with self.graph.as_default():
            dim_x = self.config.dim_x
            dim_z = self.config.dim_z
            bs = self.config.batch_size
            self.real_data_int = tf.placeholder(tf.int32, shape=[None, dim_x],
                                                name='real_data_int')
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
            real_data_t = 2*((tf.cast(self.real_data_int, tf.float32)/255.)-.5)
            real_data_NCHW = tf.reshape(real_data_t, [-1, 3, 32, 32])
            real_data_NHWC = tf.transpose(real_data_NCHW,
                                          perm=[0, 2, 3, 1])
            real_data = tf.reshape(real_data_NHWC, [-1, dim_x])
            fake_data = self.generator(self.noise)
            disc_real = self.discriminator(real_data)
            disc_fake = self.discriminator(fake_data, reuse=True)

            if self.config.gan_mode == 'dcgan':
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
                tvars_gen = [v for v in tvars if 'Generator' in v.name]
                tvars_disc = [v for v in tvars if 'Discriminator' in v.name]

                gen_train_op = tf.train.AdamOptimizer(learning_rate=lr,
                    beta1=beta1, beta2=beta2).minimize(gen_cost,
                        var_list=tvars_gen)
                disc_train_op = tf.train.AdamOptimizer(learning_rate=lr,
                    beta1=beta1, beta2=beta2).minimize(disc_cost,
                        var_list=tvars_disc)
            else:
                raise Exception('Invalid gan_mode')

            self.saver = tf.train.Saver()
            self.init = tf.global_variables_initializer()
            self.gen_train_op = gen_train_op
            self.disc_train_op = disc_train_op
            self.update = [gen_train_op, disc_train_op]
            self.disc_cost_fake = disc_cost_fake
            self.disc_cost_real = disc_cost_real
            self.gen_cost = gen_cost
            self.fake_data = fake_data

    def generator(self, input):
        dim_z = self.config.dim_z
        dim_c = self.config.dim_c
        with tf.variable_scope('Generator'):
            output = layers.linear(input, 4*4*4*dim_c, name='LN1')
            output = layers.batchnorm(output, is_training=self.is_training,
                                      name='BN1')
            output = tf.nn.relu(output)
            output = tf.reshape(output, [-1, 4, 4, 4*dim_c])

            output_shape = [-1, 8, 8, 2*dim_c]
            output = layers.deconv2d(output, output_shape, name='Deconv2')
            output = layers.batchnorm(output, is_training=self.is_training,
                                      name='BN2')
            output = tf.nn.relu(output)

            output_shape = [-1, 16, 16, dim_c]
            output = layers.deconv2d(output, output_shape, name='Deconv3')
            output = layers.batchnorm(output, is_training=self.is_training,
                                      name='BN3')
            output = tf.nn.relu(output)

            output_shape = [-1, 32, 32, 3]
            output = layers.deconv2d(output, output_shape, name='Decovn4')
            output = tf.nn.tanh(output)

            return tf.reshape(output, [-1, 32*32*3])

    def discriminator(self, inputs, reuse=False):
        dim_c = self.config.dim_c
        with tf.variable_scope('Discriminator') as scope:
            if reuse:
                scope.reuse_variables()

            output = tf.reshape(inputs, [-1, 32, 32, 3])

            output = layers.conv2d(output, dim_c, name='Conv1')
            output = tf.nn.leaky_relu(output)

            output = layers.conv2d(output, 2*dim_c, name='Conv2')
            output = layers.batchnorm(output, is_training=self.is_training,
                                      name='BN2')
            output = tf.nn.leaky_relu(output)

            output = layers.conv2d(output, 4*dim_c, name='Conv3')
            output = layers.batchnorm(output, is_training=self.is_training,
                                      name='BN3')
            output = tf.nn.leaky_relu(output)

            output = tf.reshape(output, [-1, 4*4*4*dim_c])
            output = layers.linear(output, 1, name='LN4')

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

        for step in range(config.max_training_step):
            start_time = time.time()

            # ----Update D network.----
            for i in range(config.disc_iters):
                data = self.train_dataset.next_batch(batch_size)
                x = data['input']

                z = np.random.normal(size=[batch_size, dim_z]).astype(np.float32)
                feed_dict = {self.noise: z, self.real_data_int: x,
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
                inception_score = self.get_inception_score(10)
                logger.info(inception_score)
                self.generate_images(step)
                endurance += 1
                if inception_score[0] > best_inps:
                    best_inps = inception_score[0]
                    endurance = 0
                    if save_model:
                        self.save_model(step)

            if step % print_frequency == (print_frequency - 1):
                data = self.train_dataset.next_batch(batch_size)
                x = data['input']
                z = np.random.normal(size=[batch_size, dim_z]).astype(np.float32)
                feed_dict = {self.noise: z, self.real_data_int: x,
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
        feed_dict = {self.noise: z, self.real_data_int: x,
                        self.is_training: True}
        a = np.argmax(np.array(action))
        sess.run(self.update[a], feed_dict=feed_dict)

        fetch = [self.gen_cost, self.disc_cost_real, self.disc_cost_fake]
        r = sess.run(fetch, feed_dict=feed_dict)
        gen_cost = r[0]
        disc_cost_real = r[1]
        disc_cost_fake = r[2]
        self.prst_gen_cost = r[0]
        self.prst_disc_cost_real = r[1]
        self.prst_disc_cost_fake = r[2]

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
        self.inception_score = score

    def get_state(self):
        if self.step_number == 0:
            state = [0] * self.config.dim_state_rl
        else:
            #state = [self.step_number / self.config.max_training_step,
            state = [
                     self.ema_gen_cost / 5,
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
        # 2) Collapse: action space collapse to one action (not implement yet)
        step = self.step_number
        if step % self.config.valid_frequency_stud == 0:
            self.endurance += 1
            inception_score = self.get_inception_score(100)
            inps = inception_score[0]
            logger.info('inception_score_100: {}'.format(inception_score))
            self.inception_score = inps
            if inps > self.best_inception_score:
                self.best_step = self.step_number
                self.best_inception_score = inps
                self.endurance = 0

        if self.config.stop_strategy_stud == 'prescribed_steps' and \
                step > self.config.max_training_step:
            return True
        elif self.config.stop_strategy_stud == 'exceeding_endurance' and \
                self.endurance > self.config.max_endurance_stud:
            return True
        return False

    def get_step_reward(self):
        return 0

    def get_final_reward(self):
        inps = self.best_inception_score
        reward = inps ** 2

        if self.reward_baseline is None:
            self.reward_baseline = reward
        decay = self.config.reward_baseline_decay
        adv = reward - self.reward_baseline
        #adv = min(adv, self.config.reward_max_value)
        #adv = max(adv, -self.config.reward_max_value)
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
        all_samples = ((all_samples+1.)*255./2.).astype(np.int32)
        all_samples = all_samples.reshape((-1, 32, 32, 3))
        return inception_score.get_inception_score(list(all_samples))

    def generate_images(self, step):
        feed_dict = {self.noise: self.fixed_noise_128,
                     self.is_training: False}
        samples = self.sess.run(self.fake_data, feed_dict=feed_dict)
        samples = ((samples+1.)*255./2.).astype('int32')
        task_dir = os.path.join(self.config.save_images_dir, self.exp_name)
        if not os.path.exists(task_dir):
            os.mkdir(task_dir)
        save_path = os.path.join(task_dir, 'images_{}.jpg'.format(step))
        save_images.save_images(samples.reshape((-1, 32, 32, 3)), save_path)


if __name__ == '__main__':
    root_path = os.path.dirname(os.path.realpath(__file__))
    config_path = os.path.join(root_path, 'config/' + 'gan.cfg')
    config = utils.Parser(config_path)
    gan = Gan(config)
