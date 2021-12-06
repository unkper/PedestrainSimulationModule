import random
import time

import numpy as np
import torch

from gym import Env
from gym.spaces import Discrete
from tqdm import tqdm

from rl.agents.Agent import Agent
from rl.utils.model.predict_env import PredictEnv
from rl.utils.networks.pd_network import MLPNetworkActor, MLPNetworkCritic
from rl.utils.model.model import EnsembleDynamicsModel
from rl.utils.updates import soft_update, hard_update
from rl.utils.classes import SaveNetworkMixin, Noise, Experience, ModelBasedMAAgentMixin, \
    MAAgentMixin
from rl.utils.functions import back_specified_dimension, onehot_from_logits, gumbel_softmax, flatten_data, \
    onehot_from_int, process_maddpg_experience_data, save_callback, model_based_loss_callback

MSELoss = torch.nn.MSELoss()


class DDPGAgent:
    def __init__(self, state_dim, action_dim,
                 learning_rate, discrete,
                 device, state_dims, action_dims,
                 actor_network=None, critic_network=None, actor_hidden_dim=64, critic_hidden_dim=64):
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.discrete = discrete
        self.device = device
        self.actor = MLPNetworkActor(state_dim, action_dim, discrete).to(self.device) \
            if actor_network is None else actor_network(state_dim, action_dim, actor_hidden_dim).to(self.device)
        self.target_actor = MLPNetworkActor(state_dim, action_dim, discrete).to(self.device) \
            if actor_network is None else actor_network(state_dim, action_dim, actor_hidden_dim).to(self.device)
        hard_update(self.target_actor, self.actor)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(),
                                                learning_rate)
        self.critic = MLPNetworkCritic(state_dims, action_dims).to(self.device) \
            if critic_network is None else critic_network(state_dims, action_dims, critic_hidden_dim).to(self.device)
        self.target_critic = MLPNetworkCritic(state_dims, action_dims).to(self.device) \
            if critic_network is None else critic_network(state_dims, action_dims, critic_hidden_dim).to(self.device)
        hard_update(self.target_critic, self.critic)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(),
                                                 learning_rate)
        self.noise = Noise(1 if self.discrete else action_dim)
        self.count = [0 for _ in range(action_dim)]

    def step(self, obs, explore):
        """
        Take a step forward in environment for a minibatch of observations
        Inputs:
            obs (PyTorch Variable): Observations for this agent
            explore : Whether to explore or not
            eps :
        Outputs:
            action (Pytorch Variable): Actions for this agent
        """
        if explore and self.discrete:
            action = onehot_from_int(random.randint(0, self.action_dim - 1), self.action_dim)  # 利用随机策略进行采样
        elif explore and not self.discrete:
            action = torch.Tensor(self.noise.sample())
            action = action.clamp(-1, 1)
        elif not explore and self.discrete:
            action = self.actor(torch.unsqueeze(obs, dim=0))  # 统一以一批次的形式进行输入
            action = onehot_from_logits(action)
            action = torch.squeeze(action).to(self.device)
        else:
            action = self.actor(torch.unsqueeze(obs, dim=0))
            action = action.clamp(-1, 1)
            action = torch.squeeze(action).to(self.device)
        self.count[torch.argmax(action).item()] += 1
        return action


class G_MADDPGAgent(ModelBasedMAAgentMixin, MAAgentMixin, SaveNetworkMixin, Agent):
    loss_recoder = []

    def __init__(self, env: Env = None,
                 capacity=2e6,
                 n_rol_threads=1,
                 batch_size=128,
                 learning_rate=1e-4,
                 update_frequent=50,
                 debug_log_frequent=500,
                 gamma=0.95,
                 tau=0.01,
                 actor_network=None,
                 critic_network=None,
                 actor_hidden_dim=64,
                 critic_hidden_dim=64,
                 model_hidden_dim=400,
                 env_name="training_env",

                 init_train_steps = 500,
                 network_size=7,
                 elite_size=5,
                 use_decay=False,
                 model_batch_size = 2048,
                 model_train_freq = 1000,
                 n_steps_model = 50,
                 rollout_length_range=(1, 1),
                 rollout_epoch_range=(60, 1500),
                 rollout_batch_size=256,
                 real_ratio = 0.3,
                 model_retain_epochs=300,
                 n_steps_train=3):
        '''
        环境的输入有以下几点变化，设此时有N个智能体：
        状态为(o1,o2,...,oN)
        每个状态o的形状暂定为一样，对于Actor有如下几种情况：
            类型为Discrete，输入层为1，输出层为需要动作空间数
            类型为Box，其Shape为（x1,x2,...,xn)，则输入层为x1*x2*xn
        对于Critic
        动作一般为一维的Box，则根据维数来进行转换
        :param env:
        :param capacity:
        :param batch_size:
        :param learning_rate:
        :param update_frequent:
        :param debug_log_frequent:
        :param gamma:
        :param n_steps_model:一次model_train的总step数
        :param real_ratio:真实经验的比例
        '''
        if env is None:
            raise Exception("agent should have an environment!")
        super(G_MADDPGAgent, self).__init__(env, capacity, env_name=env_name, gamma=gamma, n_rol_threads=n_rol_threads,
                                            init_train_steps=init_train_steps)
        self.state_dims = []
        for obs in env.observation_space:
            self.state_dims.append(back_specified_dimension(obs))
        action = self.env.action_space[0]
        self.discrete = type(action) is Discrete
        self.action_dims = []
        for action in env.action_space:
            if self.discrete:
                self.action_dims.append(action.n)
            else:
                self.action_dims.append(back_specified_dimension(action))
        self.actor_hidden_dim = actor_hidden_dim
        self.critic_hidden_dim = critic_hidden_dim
        self.batch_size = batch_size
        self.update_frequent = update_frequent
        self.log_frequent = debug_log_frequent
        self.learning_rate = learning_rate
        self.gamma = gamma
        self.tau = tau
        self.device = torch.device('cuda:0') if torch.cuda.is_available() else torch.device('cpu')
        self.agents = []
        self.experience = Experience(capacity)
        self.env_name = env_name
        #model-based parameters
        self.model_batch_size = model_batch_size
        self.model_retain_epochs = model_retain_epochs
        self.n_steps_train = n_steps_train
        self.model_train_freq = model_train_freq
        self.n_steps_model = n_steps_model
        self.rollout_length_range = rollout_length_range
        self.rollout_epoch_range = rollout_epoch_range
        self.rollout_length = 1
        self.rollout_batch_size = rollout_batch_size
        self.real_ratio = real_ratio
        self.l2_norm = 0.0
        self.model = EnsembleDynamicsModel(network_size, elite_size, sum(self.state_dims),
                                           sum(self.action_dims) if not self.discrete else self.env.agent_count,
                                           self.env.agent_count, model_hidden_dim, use_decay)
        self.predict_env = PredictEnv(self.model, self.env_name, 'pytorch')
        self.model_experience = Experience(capacity)

        for i in range(self.env.agent_count):
            ag = DDPGAgent(self.state_dims[i], self.action_dims[i],
                           self.learning_rate, self.discrete, self.device, self.state_dims,
                           self.action_dims, actor_network, critic_network, actor_hidden_dim,
                           critic_hidden_dim)
            self.agents.append(ag)

        self.loss_callback_ = model_based_loss_callback
        self.save_callback_ = save_callback
        return

    def __str__(self):
        return "G_Maddpg"

    def _learn_from_memory(self, trans_pieces):
        '''
        从记忆学习，更新两个网络的参数
        :return:
        '''
        # 随机获取记忆里的Transmition
        total_critic_loss = 0.0
        total_actor_loss = 0.0

        s0, a0, r1, is_done, s1, s0_critic_in, s1_critic_in = \
            process_maddpg_experience_data(trans_pieces, self.state_dims, self.env.agent_count, self.device)

        for i in range(self.env.agent_count):
            with torch.no_grad():
                if self.discrete:
                    a1 = torch.cat([onehot_from_logits(self.agents[j].target_actor.forward(s1[j])).to(self.device)
                                    for j in range(self.env.agent_count)],dim=1)
                else:
                    a1 = torch.cat([self.agents[j].target_actor.forward(s1[j]) for j in range(self.env.agent_count)],dim=1)
                # detach()的作用是让梯度无法传导到target_critic,因为此时只有critic需要更新！
                target_V = self.agents[i].target_critic.forward(s1_critic_in, a1)
            # 优化评判家网络参数，优化的目标是使评判值与r + gamma * Q'(s1,a1)尽量接近
            target_Q = r1[:, i] + self.gamma * target_V * torch.tensor(1 - is_done[:, i]).to(self.device)
            current_Q = self.agents[i].critic.forward(s0_critic_in, a0) # 此时没有使用detach！
            critic_loss = MSELoss(current_Q, target_Q).to(self.device)
            self.agents[i].critic_optimizer.zero_grad()
            critic_loss.backward()
            torch.nn.utils.clip_grad_norm_(self.agents[i].critic.parameters(), 0.5)
            self.agents[i].critic_optimizer.step()
            total_critic_loss += critic_loss.item()

            # 优化演员网络参数，优化的目标是使得Q增大
            curr_pol_out = self.agents[i].actor.forward(s0[i])
            pred_a = []
            if self.discrete:
                for j in range(self.env.agent_count):
                    pred_a.append(gumbel_softmax(curr_pol_out).to(self.device)
                                  if i == j else onehot_from_logits(self.agents[j].actor.forward(s0[j])).to(self.device))
                pred_a = torch.cat(pred_a, dim=1)
            else:
                pred_a = torch.cat([self.agents[j].actor.forward(s0[j]) for j in range(self.env.agent_count)], dim=1)
            # 反向梯度下降
            actor_loss = -1 * self.agents[i].critic.forward(s0_critic_in, pred_a).mean()
            actor_loss += (curr_pol_out ** 2).mean() * 1e-3

            self.agents[i].actor_optimizer.zero_grad()
            actor_loss.backward()
            torch.nn.utils.clip_grad_norm_(self.agents[i].actor.parameters(), 0.5)
            self.agents[i].actor_optimizer.step()
            total_actor_loss += actor_loss.item()

            soft_update(self.agents[i].target_actor, self.agents[i].actor, self.tau)
            soft_update(self.agents[i].target_critic, self.agents[i].critic, self.tau)
        return (total_critic_loss, total_actor_loss)