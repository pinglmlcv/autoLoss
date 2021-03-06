""" The module for training autoLoss """
# __Author__ == "Haowen Xu"
# __Data__ == "04-08-2018"

import tensorflow as tf
import numpy as np
import logging
import os
import sys
import math
import socket
from time import gmtime, strftime

from models import controller
from models import toy
from models import cls
from models import gan
from models import gan_grid
from models import gan_cifar10
import utils
from utils.analyse_utils import loss_analyzer_toy
from utils.analyse_utils import loss_analyzer_gan
import socket


root_path = os.path.dirname(os.path.realpath(__file__))
logger = utils.get_logger()

def discount_rewards(reward, final_reward):
    # TODO(haowen) Final reward + step reward
    reward_dis = np.array(reward) + np.array(final_reward)
    return reward_dis

def _sampling(num):
    p = np.random.rand(1)
    p = min(p, 1-1e-9)
    return int(p * num)

class Trainer():
    """ A class to wrap training code. """
    def __init__(self, config, exp_name=None, arch=None):
        self.config = config

        hostname = socket.gethostname()
        hostname = '-'.join(hostname.split('.')[0:2])
        datetime = strftime('%m-%d-%H-%M', gmtime())
        if not exp_name:
            exp_name = '{}_{}'.format(hostname, datetime)
        logger.info('exp_name: {}'.format(exp_name))

        self.model_ctrl = controller.Controller(config, exp_name+'_ctrl')
        if config.student_model_name == 'toy':
            self.model_stud = toy.Toy(config, exp_name+'_reg')
        elif config.student_model_name == 'cls':
            self.model_stud = cls.Cls(config, exp_name+'_cls')
        elif config.student_model_name == 'gan':
            self.model_stud = gan.Gan(config, exp_name+'_gan', arch=arch)
        elif config.student_model_name == 'gan_grid':
            self.model_stud = gan_grid.Gan_grid(config, exp_name+'_gan_grid')
        elif config.student_model_name == 'gan_cifar10':
            self.model_stud = gan_cifar10.Gan_cifar10(config,
                                                      exp_name+'_gan_cifar10')
        else:
            raise NotImplementedError

    def train(self, save_ctrl=None, load_ctrl=None):
        """ Iteratively training between controller and the student model """
        config = self.config
        lr = config.lr_rl
        model_ctrl = self.model_ctrl
        model_stud = self.model_stud
        best_reward = -1e5
        best_acc = 0
        best_loss = 0
        best_inps = 0
        best_best_inps = 0
        endurance = 0

        # ----Initialize controllor.----
        model_ctrl.initialize_weights()
        if load_ctrl:
            model_ctrl.load_model(load_ctrl)

        # ----Initialize gradient buffer.----
        gradBuffer = model_ctrl.get_weights()
        for ix, grad in enumerate(gradBuffer):
            gradBuffer[ix] = grad * 0

        # ----Start episodes.----
        for ep in range(config.total_episodes):
            logger.info('=================')
            logger.info('episodes: {}'.format(ep))

            # ----Initialize student model.----
            model_stud.initialize_weights()
            model_stud.reset()
            model_ctrl.print_weights()

            state = model_stud.get_state()
            state_hist = []
            action_hist = []
            reward_hist = []
            valid_loss_hist = []
            train_loss_hist = []
            old_action = []
            gen_cost_hist = []
            disc_cost_real_hist = []
            disc_cost_fake_hist = []
            step = -1
            # ----Running one episode.----
            while True:
                step += 1
                explore_rate = config.explore_rate_rl *\
                    math.exp(-ep / config.explore_rate_decay_rl)
                action = model_ctrl.sample(state, explore_rate=explore_rate)
                state_new, reward, dead = model_stud.response(action)
                # ----Record training details.----
                state_hist.append(state)
                action_hist.append(action)
                reward_hist.append(reward)
                if 'gan' in config.student_model_name:
                    gen_cost_hist.append(model_stud.ema_gen_cost)
                    disc_cost_real_hist.append(model_stud.ema_disc_cost_real)
                    disc_cost_fake_hist.append(model_stud.ema_disc_cost_fake)
                else:
                    valid_loss_hist.append(model_stud.previous_valid_loss[-1])
                    train_loss_hist.append(model_stud.previous_train_loss[-1])

                # ----Print training details.----
                #if step < 200:
                #    logger.info('----train_step: {}----'.format(step))
                #    logger.info('state:{}'.format(state_new))
                #    logger.info('action: {}'.format(action))
                #    logger.info('reward:{}'.format(reward))
                #    lv = model_stud.previous_valid_loss
                #    lt = model_stud.previous_train_loss
                #    av = model_stud.previous_valid_acc
                #    at = model_stud.previous_train_acc
                #    logger.info('train_loss: {}'.format(lt[-1]))
                #    logger.info('valid_loss: {}'.format(lv[-1]))
                #    logger.info('loss_imp: {}'.format(lv[-2] - lv[-1]))
                #    logger.info('train_acc: {}'.format(at[-1]))
                #    logger.info('valid_acc: {}'.format(av[-1]))
                #    model_stud.print_weights()

                old_action = action
                state = state_new
                if dead:
                    break

            # ----Use the best performance model to get inception score on a
            #     larger number of samples to reduce the variance of reward----
            if config.student_model_name == 'gan' and model_stud.task_dir:
                model_stud.load_model(model_stud.task_dir)
                inps_test = model_stud.get_inception_score(5000)
                logger.info('inps_test: {}'.format(inps_test))
                model_stud.update_inception_score(inps_test[0])
            else:
                logger.info('student model hasn\'t been saved before')


            # ----Update the controller.----
            final_reward, adv = model_stud.get_final_reward()
            reward_hist = np.array(reward_hist)
            reward_hist = discount_rewards(reward_hist, adv)
            sh = np.array(state_hist)
            ah = np.array(action_hist)
            rh = np.array(reward_hist)
            grads = model_ctrl.get_gradients(sh, ah, rh)
            for idx, grad in enumerate(grads):
                gradBuffer[idx] += grad

            if lr > config.lr_rl * 0.1:
                lr = lr * config.lr_decay_rl
            if ep % config.update_frequency == (config.update_frequency - 1):
                logger.info('UPDATE CONTROLLOR')
                logger.info('lr_ctrl: {}'.format(lr))
                model_ctrl.train_one_step(gradBuffer, lr)
                logger.info('grad')
                for ix, grad in enumerate(gradBuffer):
                    logger.info(grad)
                    gradBuffer[ix] = grad * 0
                logger.info('weights')
                model_ctrl.print_weights()

                # ----Print training details.----
                #logger.info('Outputs')
                #index = []
                #ind = 1
                #while ind < len(state_hist):
                #    index.append(ind-1)
                #    ind += 2000
                #feed_dict = {model_ctrl.state_plh:np.array(state_hist)[index],
                #            model_ctrl.action_plh:np.array(action_hist)[index],
                #            model_ctrl.reward_plh:np.array(reward_hist)[index]}
                #fetch = [model_ctrl.output,
                #         model_ctrl.action,
                #         model_ctrl.reward_plh,
                #         model_ctrl.state_plh,
                #         model_ctrl.logits
                #        ]
                #r = model_ctrl.sess.run(fetch, feed_dict=feed_dict)
                #logger.info('state:\n{}'.format(r[3]))
                #logger.info('output:\n{}'.format(r[0]))
                #logger.info('action: {}'.format(r[1]))
                #logger.info('reward: {}'.format(r[2]))
                #logger.info('logits: {}'.format(r[4]))

            save_model_flag = False
            if config.student_model_name == 'toy':
                loss_analyzer_toy(action_hist, valid_loss_hist,
                                  train_loss_hist, reward_hist)
                loss = model_stud.best_loss
                if final_reward > best_reward:
                    best_reward = final_reward
                    best_loss = loss
                    save_model_flag = Ture
                logger.info('best_loss: {}'.format(loss))
                logger.info('lambda1: {}'.format(config.lambda1_stud))
            elif config.student_model_name == 'cls':
                loss_analyzer_toy(action_hist, valid_loss_hist,
                                  train_loss_hist, reward_hist)
                acc = model_stud.best_acc
                loss = model_stud.best_loss
                if final_reward > best_reward:
                    best_reward = final_reward
                    best_acc = acc
                    best_loss = loss
                    save_model_flag = True
                logger.info('acc: {}'.format(acc))
                logger.info('best_acc: {}'.format(best_acc))
                logger.info('best_loss: {}'.format(loss))
                #if ep % config.save_frequency == 0 and ep > 0:
                #    save_model_flag = True
            elif config.student_model_name == 'gan' or\
                config.student_model_name == 'gan_cifar10':
                loss_analyzer_gan(action_hist, reward_hist)
                best_inps = model_stud.best_inception_score
                if final_reward > best_reward:
                    best_reward = final_reward
                    best_best_inps = best_inps
                    save_model_flag = True
                logger.info('best_inps: {}'.format(best_inps))
                logger.info('best_best_inps: {}'.format(best_best_inps))
                logger.info('final_inps_baseline: {}'.\
                            format(model_stud.final_inps_baseline))
            elif config.student_model_name == 'gan_grid':
                loss_analyzer_gan(action_hist, reward_hist)
                hq_ratio = model_stud.best_hq_ratio
                if final_reward > best_reward:
                    best_reward = final_reward
                    best_hq_ratio = hq_ratio
                    save_model_flag = True
                logger.info('hq_ratio: {}'.format(hq_ratio))
                logger.info('best_hq_ratio: {}'.format(best_hq_ratio))

            logger.info('adv: {}'.format(adv))

            if save_model_flag and save_ctrl:
                model_ctrl.save_model(ep)

    def test(self, load_ctrl, ckpt_num=None):
        config = self.config
        model_ctrl = self.model_ctrl
        model_stud = self.model_stud
        model_ctrl.initialize_weights()
        model_stud.initialize_weights()
        model_ctrl.load_model(load_ctrl, ckpt_num=ckpt_num)
        model_stud.reset()

        state = model_stud.get_state()
        for i in range(config.max_training_step):
            action = model_ctrl.sample(state)
            state_new, _, dead = model_stud.response(action)
            state = state_new
            if dead:
                break
        if config.student_model_name == 'toy':
            raise NotImplementedError
        elif config.student_model_name == 'cls':
            valid_acc = model_stud.best_acc
            test_acc = model_stud.test_acc
            logger.info('valid_acc: {}'.format(valid_acc))
            logger.info('test_acc: {}'.format(test_acc))
            return test_acc
        elif config.student_model_name == 'gan':
            model_stud.load_model(model_stud.task_dir)
            inps_test = model_stud.get_inception_score(5000)
            logger.info('inps_test: {}'.format(inps_test))
            return inps_test
        elif config.student_model_name == 'gan_grid':
            raise NotImplementedError
        elif config.student_model_name == 'gan_cifar10':
            best_inps = model_stud.best_inception_score
            logger.info('best_inps: {}'.format(best_inps))
            return best_inps
        else:
            raise NotImplementedError

    def baseline(self):
        self.model_stud.initialize_weights()
        self.model_stud.train(save_model=True)
        if self.config.student_model_name == 'gan':
            self.model_stud.load_model(self.model_stud.task_dir)
            inps_baseline = self.model_stud.get_inception_score(5000)
            logger.info('inps_baseline: {}'.format(inps_baseline))
        return inps_baseline



if __name__ == '__main__':
    # ----Parsing config file.----
    logger.info(socket.gethostname())
    if len(sys.argv) > 1:
        config_file = sys.argv[1]
    else:
        config_file = 'gan.cfg'
    config_path = os.path.join(root_path, 'config/' + config_file)
    config = utils.Parser(config_path)
    config.print_config()

    # ----Sample an gan architecture.----
    dim_c_G_cand = [32, 64, 128]
    dim_c_D_cand = [32, 64, 128]
    dim_z_cand = [64, 128]
    batchnorm_G_cand = [True, False]
    batchnorm_D_cand = [True, False]
    activation_G_cand = ['relu', 'leakyRelu']
    activation_D_cand = ['relu', 'leakyRelu']
    depth_G_cand = [2, 3, 4]
    depth_D_cand = [2, 3, 4]
    architecture = {}
    architecture['dim_c_G'] = dim_c_G_cand[_sampling(3)]
    architecture['dim_c_D'] = dim_c_D_cand[_sampling(3)]
    architecture['dim_z'] = dim_z_cand[_sampling(2)]
    architecture['batchnorm_G'] = batchnorm_G_cand[_sampling(2)]
    architecture['batchnorm_D'] = batchnorm_D_cand[_sampling(2)]
    architecture['activation_G'] = activation_G_cand[_sampling(2)]
    architecture['activation_D'] = activation_D_cand[_sampling(2)]
    architecture['depth_G'] = depth_G_cand[_sampling(3)]
    architecture['depth_D'] = depth_D_cand[_sampling(3)]

    # ----Instantiate a trainer object.----
    trainer = Trainer(config, arch=architecture)

    # classification task controllor model
    #load_ctrl = '/datasets/BigLearning/haowen/autoLoss/saved_models/h5-haowen6_05-13-04-30_ctrl'
    # gan mnist task controllor model
    load_ctrl = '/datasets/BigLearning/haowen/autoLoss/saved_models/h2-haowen6_05-13-20-21_ctrl'
    # ----Training----
    #   --Start from pretrained--
    #trainer.train(load_ctrl=load_ctrl)
    #   --Start from strach--
    #trainer.train(save_ctrl=True)
    #trainer.train()

    # ----Baseline----
    logger.info('BASELINE')
    baseline_accs = []
    for i in range(1):
        baseline_accs.append(trainer.baseline())
    logger.info(baseline_accs)

    # ----Testing----
    logger.info('TEST')
    test_accs = []
    #ckpt_num = 250
    ckpt_num = None
    for i in range(1):
        test_accs.append(trainer.test(load_ctrl, ckpt_num=ckpt_num))
    logger.info(test_accs)

