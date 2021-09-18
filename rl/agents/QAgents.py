import copy
import os
import time
from collections import defaultdict

import numpy as np
import random as randomlib
import torch
import torch.nn as nn
import torch.nn.functional as F

from random import random
from gym import Env
from gym.spaces import Discrete

from rl.agents.Agent import Agent
from rl.utils.networks.pd_network import NetApproximator
from rl.utils.functions import get_dict, set_dict, back_specified_dimension, process_experience_data, print_train_string
from rl.utils.policys import epsilon_greedy_policy, greedy_policy, deep_epsilon_greedy_policy
from rl.utils.classes import SaveDictMixin, SaveNetworkMixin, Transition, Experience
from rl.utils.updates import soft_update

class QAgent(Agent,SaveDictMixin):
    def __init__(self,env:Env,capacity:int = 20000):
        super(QAgent, self).__init__(env,capacity)
        self.name = "QAgent"
        self.Q = {}

    def policy(self,A ,s = None,Q = None, epsilon = None):
        return epsilon_greedy_policy(A, s, Q, epsilon)

    def learning_method(self,lambda_ = None,gamma = 0.9,alpha = 0.1,
                        epsilon = 1e-5,display = False,wait = False,waitSecond:float = 0.01):
        self.state = self.env.reset()
        s0 = self.state
        if display:
            self.env.render()
        time_in_episode, total_reward = 0, 0
        is_done = False
        while not is_done:
            #行动部分
            self.policy = epsilon_greedy_policy
            a0 = self.perform_policy(s0,self.Q, epsilon)
            s1, r1, is_done, info ,total_reward = self.act(a0)
            if display:
                self.env.render()
            #估值部分
            self.policy = greedy_policy
            a1 = greedy_policy(self.A, s1, self.Q)
            old_q = get_dict(self.Q,s0,a0)
            q_prime = get_dict(self.Q, s1, a1) #得到下一个状态，行为的估值
            td_target = r1 + gamma * q_prime
            new_q = old_q + alpha * (td_target - old_q)
            set_dict(self.Q, new_q, s0, a0)

            s0 = s1
            time_in_episode += 1
            if wait:
                time.sleep(waitSecond)

        print(self.experience.last_episode)
        return time_in_episode,total_reward

    def play_init(self,savePath,s0):
        self.Q = self.load_obj(savePath)
        return int(greedy_policy(self.A,s0,self.Q))

    def play_step(self,savePath,s0):
        return int(greedy_policy(self.A,s0,self.Q))

class DQNAgent(Agent,SaveNetworkMixin):
    def __init__(self,env:Env = None,
                    capacity = 20000,
                    hidden_dim:int = 32,
                    batch_size = 128,
                    epochs = 2,
                    gamma = 0.95,
                    learning_rate = 1e-4,
                    tau:float = 0.4,
                    ddqn:bool = True,
                    network:nn.Module = None):
        if env is None:
            raise Exception("agent should have an environment!")
        super(DQNAgent, self).__init__(env,capacity)
        self.name = "DQNAgent"
        self.input_dim = back_specified_dimension(env.observation_space)
        if type(env.action_space) is Discrete:
            self.output_dim = env.action_space.n
        else:
            raise Exception("DQN只能处理动作空间为Discrete的智能体!")
        self.hidden_dim = hidden_dim
        self.device = torch.device("cpu") if not torch.cuda.is_available() else torch.device("cuda:0")
        # 行为网络，该网络用来计算产生行为，以及对应的Q值，参数频繁更新
        self.behavior_Q = network(input_dim=self.input_dim, output_dim=self.output_dim) \
                                    if network else NetApproximator(input_dim=self.input_dim,
                                                                    output_dim=self.output_dim,
                                                                    hidden_dim = self.hidden_dim)
        self.behavior_Q.to(self.device)
        #计算目标价值的网络，两者在初始时参数一致，该网络参数不定期更新
        self.target_Q = self.behavior_Q.clone()

        self.batch_size = batch_size
        self.epochs = epochs
        self.tau = tau
        self.learning_rate = learning_rate
        self.gamma = gamma

        self.ddqn = ddqn
        if self.ddqn:print("使用DDQN算法更新Q值!")

    def _update_target_Q(self):
        # 使用软更新策略来缓解不稳定的问题
        soft_update(self.target_Q, self.behavior_Q, self.tau)

    def policy(self,A ,s = None,Q = None, epsilon = None):
        return deep_epsilon_greedy_policy(s, epsilon, self.env, self.behavior_Q)

    def learning_method(self,epsilon = 1e-5,display = False,wait = False,waitSecond:float = 0.01):
        self.state = self.env.reset()
        if display:
            self.env.render()
        time_in_episode, total_reward = 0, 0
        is_done = False
        loss = 0
        while not is_done:
            s0 = self.state
            a0 = self.perform_policy(s0, epsilon=epsilon)
            s1, r1, is_done, info, total_reward = self.act(a0)
            if display:
                self.env.render()
            if self.total_trans > self.batch_size: #and time_in_episode % self.update_frequent == 0:
                loss += self._learn_from_memory(self.gamma, self.learning_rate)
            if time_in_episode > 5000: #防止CartPole长时间进行训练
                is_done = True
            time_in_episode += 1

        loss /= time_in_episode
        if self.total_episodes_in_train % (self.max_episode_num // 20) == 0:
            print_train_string(self.experience, self.max_episode_num // 20)
        return time_in_episode, total_reward, loss

    def _learn_from_memory(self, gamma, learning_rate):
        trans_pieces = self.sample(self.batch_size)
        states_0, actions_0, \
        reward_1, is_done, states_1 = process_experience_data(trans_pieces)

        #准备训练数据
        X_batch = states_0
        y_batch = self.target_Q(states_0)
        Q_target = reward_1 + gamma * np.max(self.target_Q(states_1), axis=1)*\
                   (~ is_done) # is_done则Q_target==reward_1

        if self.ddqn:
            #行为a'从行为价值网络中得到
            a_prime = np.argmax(self.behavior_Q(states_1), axis=1).reshape(-1)
            #(s',a')的价值从目标价值网络中得到
            Q_states_1 = self.target_Q(states_1)
            temp_Q = Q_states_1[np.arange(len(Q_states_1)), a_prime]
            # (s, a)的目标价值根据贝尔曼方程得到
            Q_target = reward_1 + gamma * temp_Q * (~ is_done)
            # is_done则Q_target==reward_1
            ##DDQN算法尾部

        y_batch[np.arange(len(X_batch)), actions_0] = Q_target

        X_batch = torch.from_numpy(X_batch)
        y_batch = torch.from_numpy(y_batch)

        #训练行为价值网络，更新其参数
        loss = self.behavior_Q.fit(x = X_batch,
                                   y = y_batch,
                                   learning_rate = learning_rate,
                                   epochs=self.epochs)
        mean_loss = loss.sum().item() / self.batch_size
        if self.total_episodes_in_train % self.update_frequent == 0:
            self._update_target_Q()
        return mean_loss

    def play_init(self,savePath,s0):
        self.load(os.path.join(savePath,"DQNAgent.pkl"),self.behavior_Q)
        return int(np.argmax(self.behavior_Q(s0)))

    def play_step(self,savePath,s0):
        return int(np.argmax(self.behavior_Q(s0)))

class Deep_DYNA_QAgent(Agent, SaveNetworkMixin):
    def __init__(self, env:Env,
                 Q_net:nn.Module,
                 model_net:nn.Module,
                 hidden_dim=32,
                 batch_size:int=128,
                 tau: float = 0.4,
                 capacity:int = 20000,
                 plan_capacity:int = 10000,
                 lookahead:int=10,
                 epochs:int=2,
                 learning_rate:float = 1e-4,
                 ddqn = True
                 ):
        super(Deep_DYNA_QAgent, self).__init__(env, capacity)
        self.name = "Deep_DYNA_QAgent"
        self.device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

        self.input_dim = back_specified_dimension(env.observation_space)
        if type(env.action_space) is Discrete:
            self.output_dim = env.action_space.n
        else:
            raise Exception("DDQ只能处理动作空间为Discrete的智能体!")

        self.Q = Q_net(self.input_dim, self.output_dim, hidden_dim).to(self.device)
        self.target_Q = copy.deepcopy(self.Q)
        self.Q_optimizer = torch.optim.Adam(self.Q.parameters(), learning_rate)
        #model中输入的a必然是Discrete的，那么动作维为一
        self.model = model_net(self.input_dim, 1, hidden_dim).to(self.device)
        self.model_optimizer = torch.optim.Adam(self.model.parameters(), learning_rate)

        self.lookahead = lookahead
        self.batch_size = batch_size
        self.tau = tau
        self.epochs = epochs
        self.ddqn = ddqn

        self.plan_experience = Experience(capacity=plan_capacity)

    def policy(self,A ,s = None,Q = None, epsilon = None):
        s = torch.from_numpy(s).float().to(self.device)
        return deep_epsilon_greedy_policy(s, epsilon, self.env, self.Q)

    def learning_method(self,lambda_ = None,gamma = 0.9,alpha = 0.1,
                        epsilon = 1e-5,display = False,wait = False,waitSecond:float = 0.01):
        self.state = self.env.reset()
        s0 = self.state
        if display:
            self.env.render()
        time_in_episode, total_reward = 0, 0
        is_done = False
        loss = 0.0
        while not is_done:
            #行动部分
            a0 = self.perform_policy(s0, self.Q, epsilon)
            s1, r1, is_done, info, total_reward = self.act(a0)
            if display:
                self.env.render()
            if wait:
                time.sleep(waitSecond)
            if time_in_episode > 5000: #防止CartPole长时间进行训练
                is_done = True
            time_in_episode += 1
            if self.total_trans > self.batch_size:
                loss += self._learn_from_memory(gamma, self._sample_from_du)
                self._learn_simulate_world()
                self._planning(gamma)
                if self.total_trans_in_train % 100 == 0:
                    self._update_target_Q()
            s0 = s1
        if self.total_episodes_in_train % (self.max_episode_num // 10) == 0:
            print_train_string(self.experience, self.max_episode_num // 10)
        loss /= time_in_episode

        return time_in_episode,total_reward, loss

    def _update_target_Q(self):
        # 使用软更新策略来缓解不稳定的问题
        soft_update(self.target_Q, self.Q, self.tau)

    def _sample_from_du(self, count=0):
        '''
        从经验中抽取值
        :return:
        '''
        return self.sample(self.batch_size if count == 0 else count)

    def _sample_from_ds(self, count=0):
        '''
        从planning经验中抽取值
        :return:
        '''
        return self.plan_experience.sample(self.batch_size if count == 0 else count)

    def _learn_from_memory(self, gamma, sample_func):
        trans_pieces = sample_func(self.batch_size)
        states_0, actions_0, \
        reward_1, is_done, states_1 = process_experience_data(trans_pieces, to_tensor=True, device=self.device)

        # 准备训练数据
        X_batch = states_0
        y_batch = self.target_Q(states_0)
        actions_0 = actions_0.long()
        Q_target = reward_1 + gamma * torch.max(self.target_Q(states_1).detach().cpu(), dim=1).values * \
                   (~ is_done)  # is_done则Q_target==reward_1
        Q_target = Q_target.to(self.device)

        if self.ddqn:
            reward_1 = reward_1.float().to(self.device)
            is_done = is_done.to(self.device)
            #行为a'从行为价值网络中得到
            a_prime = torch.argmax(self.Q(states_1), dim=1)
            #(s',a')的价值从目标价值网络中得到
            Q_states_1 = self.target_Q(states_1)
            temp_Q = Q_states_1[np.arange(len(Q_states_1)), a_prime]
            # (s, a)的目标价值根据贝尔曼方程得到
            Q_target = reward_1 + gamma * temp_Q * (~ is_done)
            # is_done则Q_target==reward_1
            ##DDQN算法尾部

        y_batch[torch.arange(len(X_batch)).data, actions_0] = Q_target
        y_pred = self.Q(X_batch)

        # 训练行为价值网络，更新其参数
        loss = F.mse_loss(y_pred, y_batch)
        self.Q_optimizer.zero_grad()
        loss.backward()
        self.Q_optimizer.step()

        mean_loss = loss.sum().item() / self.batch_size
        # will update later!
        # if self.experience.total_trans % 100 == 0:
        #     self._update_target_Q()
        return mean_loss

    def _learn_simulate_world(self):
        trans_pieces = self.sample(self.batch_size)
        states_0, actions_0, \
        reward_1, is_done, states_1 = process_experience_data(trans_pieces, to_tensor=True, device=self.device)

        reward_1 = torch.unsqueeze(reward_1, dim=1).to(self.device)
        is_done = torch.unsqueeze(is_done, dim=1).float().to(self.device)
        #world model输入为(s,a)，输出为(s',r, is_done)
        _state_1, _reward, _is_done = self.model(states_0, actions_0)

        self.model_optimizer.zero_grad()
        loss = F.mse_loss(_state_1, states_1) + \
                F.mse_loss(_reward, reward_1) + \
                F.binary_cross_entropy_with_logits(_is_done, is_done)
        loss.backward()
        self.model_optimizer.step()

        mean_loss = loss.sum().item() / self.batch_size
        return mean_loss

    def _planning(self, gamma):
        trans_piece = self._sample_from_du(self.lookahead)
        states_0, actions_0, _, _, _ = process_experience_data(trans_piece)

        states_0_in = torch.from_numpy(states_0).float().to(self.device)
        actions_0_in = torch.from_numpy(actions_0).float().to(self.device)
        rewards, states_1, is_done = self.model(states_0_in, actions_0_in)
        rewards = rewards.detach().cpu().numpy()
        states_1 = states_1.detach().cpu().numpy()
        is_done = is_done.detach().cpu().numpy()

        for i in range(len(states_0)):
            trans = Transition(states_0[i], actions_0[i], rewards[i], is_done[i], states_1[i])
            self.plan_experience.push(trans)
        #从planning经验中开始学习
        if self.plan_experience.len >= self.batch_size:
            self._learn_from_memory(gamma, self._sample_from_ds)

    def play_init(self,savePath,s0):
        self.load(savePath,self.Q)
        s0 = torch.from_numpy(s0).to(self.device)
        return int(np.argmax(self.Q(s0)))

    def play_step(self,savePath,s0):
        s0 = torch.from_numpy(s0).to(self.device)
        return int(np.argmax(self.Q(s0)))

