import random
import time

import torch
import torch.nn.functional as F
import numpy as np

from gym import Env
from gym.spaces import Discrete

from rl.agents.Agent import Agent
from rl.utils.miscellaneous import CUDA_DEVICE_ID
from rl.utils.networks.maddpg_network import MLPNetworkActor, DoubleQNetworkCritic
from rl.utils.updates import soft_update, hard_update
from rl.utils.classes import SaveNetworkMixin, Noise, Experience, MAAgentMixin, PedsMoveInfoDataHandler
from rl.utils.functions import back_specified_dimension, onehot_from_logits, gumbel_softmax, flatten_data, \
    onehot_from_int, save_callback, process_maddpg_experience_data, loss_callback, info_callback

MSELoss = torch.nn.MSELoss()

class DDPGAgent:
    def __init__(self, state_dim, action_dim,
                 learning_rate, discrete,
                 device, state_dims, action_dims,
                 actor_network = None, critic_network = None, actor_hidden_dim=64, critic_hidden_dim=64):
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.discrete = discrete
        self.device = device
        self.actor = MLPNetworkActor(state_dim, action_dim, discrete).to(self.device) \
            if actor_network == None else actor_network(state_dim, action_dim, discrete, actor_hidden_dim).to(self.device)
        self.target_actor = MLPNetworkActor(state_dim, action_dim, discrete).to(self.device) \
            if actor_network == None else actor_network(state_dim, action_dim, discrete, actor_hidden_dim).to(self.device)
        hard_update(self.target_actor, self.actor)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), learning_rate)

        self.critic = DoubleQNetworkCritic(state_dims, action_dims).to(self.device)\
            if critic_network == None else critic_network(state_dims, action_dims, critic_hidden_dim).to(self.device)
        self.target_critic = DoubleQNetworkCritic(state_dims, action_dims).to(self.device)\
            if critic_network == None else critic_network(state_dims, action_dims, critic_hidden_dim).to(self.device)
        hard_update(self.target_critic, self.critic)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), learning_rate)

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
            action = onehot_from_int(random.randint(0, self.action_dim - 1), self.action_dim)  # ??????????????????????????????
        elif explore and not self.discrete:
            action = torch.Tensor(self.noise.sample()).to(self.device)
            action = action.clamp(-1, 1)
        elif not explore and self.discrete:
            action = self.actor(torch.unsqueeze(obs, dim=0))  # ???????????????????????????????????????
            action = onehot_from_logits(action)
            action = torch.squeeze(action).to(self.device)
        else:
            action = self.actor(torch.unsqueeze(obs, dim=0))
            action = action.clamp(-1, 1)
            action = torch.squeeze(action).to(self.device)
        self.count[torch.argmax(action).item()] += 1
        return action

class MATD3Agent(MAAgentMixin, SaveNetworkMixin, Agent):
    loss_recoder = []

    def __init__(self, env: Env = None,
                 capacity=2e6,
                 n_rol_threads = 1,
                 batch_size=128,
                 learning_rate=1e-4,
                 update_frequent = 4,
                 debug_log_frequent = 500,
                 gamma = 0.95,
                 tau = 0.01,
                 K = 5,
                 log_dir="./",
                 actor_network = None,
                 critic_network = None,
                 actor_hidden_dim = 64,
                 critic_hidden_dim = 64,
                 n_steps_train=5,
                 env_name = "training_env",

                 demo_experience:Experience=None,
                 batch_size_d = 128,
                 lambda_1 = 0.001,
                 lambda_2 = 0.0078
                 ):
        '''
        ???????????????????????????????????????????????????N???????????????
        ?????????(o1,o2,...,oN)
        ????????????o?????????????????????????????????Actor????????????????????????
            ?????????Discrete???????????????1????????????????????????????????????
            ?????????Box??????Shape??????x1,x2,...,xn)??????????????????x1*x2*xn
        ??????Critic
        ????????????????????????Box?????????????????????????????????
        :param env:
        :param capacity:
        :param batch_size:
        :param learning_rate:
        :param update_frequent:
        :param debug_log_frequent:
        :param gamma:
        '''
        if env is None:
            raise Exception("agent should have an environment!")
        super(MATD3Agent, self).__init__(env, capacity, env_name=env_name,  gamma=gamma, n_rol_threads=n_rol_threads,
                                         log_dir=log_dir)
        self.state_dims = []
        for obs in env.observation_space:
            self.state_dims.append(back_specified_dimension(obs))
        # ?????????????????????????????????????????????????????????Box???Space?????????!
        action = self.env.action_space[0]
        self.discrete = type(action) is Discrete
        self.action_dims = []
        for action in env.action_space:
            if self.discrete:
                self.action_dims.append(action.n)
            else:
                self.action_dims.append(back_specified_dimension(action))
        self.n_rol_threads = n_rol_threads
        self.actor_hidden_dim = actor_hidden_dim
        self.critic_hidden_dim = critic_hidden_dim
        self.batch_size = batch_size
        self.update_frequent = update_frequent
        self.log_frequent = debug_log_frequent
        self.learning_rate = learning_rate
        self.gamma = gamma
        self.tau = tau
        self.n_steps_train = n_steps_train
        self.train_update_count = 0
        self.K = K
        self.device = torch.device('cuda:'+str(CUDA_DEVICE_ID)) if torch.cuda.is_available() else torch.device('cpu')
        self.agents = []
        self.experience = Experience(capacity)
        for i in range(self.env.agent_count):
            ag = DDPGAgent(self.state_dims[i], self.action_dims[i],
                           self.learning_rate, self.discrete, self.device, self.state_dims,
                           self.action_dims, actor_network, critic_network, self.actor_hidden_dim, self.critic_hidden_dim)
            self.agents.append(ag)

        self.batch_size_d = batch_size_d
        if demo_experience:
            if self.batch_size_d > self.batch_size:
                raise Exception("????????????????????????????????????????????????!")
            #self.batch_size -= self.batch_size_d #???????????????????????????
            print("????????????????????????????????????!")
        self.demo_experience = demo_experience
        self.lambda_1 = lambda_1
        self.lambda_2 = lambda_2

        self.info_handler = PedsMoveInfoDataHandler(env.terrain, env.agent_count)
        self.info_callback_ = info_callback
        self.loss_callback_ = loss_callback
        self.save_callback_ = save_callback
        return

    def __str__(self):
        return "Matd3"

    def _learn_from_memory(self, trans_pieces, BC=False):
        '''
        ?????????????????????????????????????????????
        :return:
        '''
        # ????????????????????????Transmition
        total_critic_loss = 0.0
        total_loss_actor = 0.0

        s0, a0, r1, is_done, s1, s0_critic_in, s1_critic_in = \
            process_maddpg_experience_data(trans_pieces, self.state_dims, self.env.agent_count, self.device)

        if BC and self.discrete:
            temp_a = np.array([x.a0 for x in trans_pieces])
            int_a0 = torch.from_numpy(np.argmax(temp_a, axis=2)).to(self.device)

        for i in range(self.env.agent_count):
            with torch.no_grad():
                if self.discrete:
                    a1 = torch.cat([onehot_from_logits(self.agents[j].target_actor.forward(s1[j])).to(self.device)
                                    for j in range(self.env.agent_count)],dim=1)
                else:
                    a1 = torch.cat([self.agents[j].target_actor.forward(s1[j])
                                    for j in range(self.env.agent_count)],dim=1)
                # detach()????????????????????????????????????target_critic,??????????????????critic???????????????
                target_Q1, target_Q2 = self.agents[i].target_critic.forward(s1_critic_in, a1)
                # ?????????????????????????????????????????????????????????TD??????
                target_V = torch.min(target_Q1, target_Q2)
            # ?????????????????????????????????????????????????????????????????????r + gamma * Q'(s1,a1)????????????
            target_Q = r1[:,i] + self.gamma * target_V * torch.tensor(1 - is_done[:,i]).to(self.device)
            current_Q1, current_Q2 = self.agents[i].critic.forward(s0_critic_in, a0)  # ??????????????????detach???
            critic_loss = MSELoss(current_Q1, target_Q) + MSELoss(current_Q2, target_Q)
            self.agents[i].critic_optimizer.zero_grad()
            critic_loss.backward()
            torch.nn.utils.clip_grad_norm_(self.agents[i].critic.parameters(), 0.5)
            self.agents[i].critic_optimizer.step()
            total_critic_loss += critic_loss.item()

            # ??????K??????????????????????????????????????????????????????
            if self.train_update_count % self.K == 0:
                # ???????????????????????????????????????????????????Q??????
                curr_pol_out = self.agents[i].actor.forward(s0[i])
                pred_a = []
                if self.discrete:
                    for j in range(self.env.agent_count):
                        pred_a.append(gumbel_softmax(curr_pol_out).to(self.device)
                                      if i == j else onehot_from_logits(self.agents[j].actor.forward(s0[j])).to(self.device))
                    pred_a = torch.cat(pred_a, dim=1)
                else:
                    pred_a = torch.cat([self.agents[j].actor.forward(s0[j])
                                    for j in range(self.env.agent_count)],dim=1)
                # ??????????????????
                if BC:
                    s_low, s_high = self.state_dims[i] * i + 2, self.state_dims[i] * i + 6  # ??????leader?????????????????????????????????
                    not_end_idx = (torch.sum(s0_critic_in[:, s_low:s_high], dim=1) != 0.0).cpu().numpy()  # ???????????????????????????????????????
                    # curr_pol_out [128,9] Q,Q_real [128,] int_a0 [128,6_map11_use]
                    Q = self.agents[i].critic.Q1(s0_critic_in, pred_a)
                    Q_demo = self.agents[i].critic.Q1(s0_critic_in, a0)
                    # use Q filter
                    _idx = (Q < Q_demo).detach().cpu().numpy()
                    idx = np.logical_and(_idx, not_end_idx)
                    idx_len = int(np.sum(idx))
                    if idx_len <= 1:
                        continue
                    else:
                        #calculate BC Loss
                        if self.discrete:
                            bc_loss = F.cross_entropy(curr_pol_out[idx,:], torch.squeeze(int_a0[idx, i]))
                        else:
                            bc_loss = F.mse_loss(pred_a[idx,:], a0[idx,:])
                        actor_loss = -self.lambda_1 * Q.mean() + self.lambda_2 * bc_loss
                        actor_loss += (curr_pol_out ** 2).mean() * 1e-3
                else:
                    actor_loss = -1 * self.agents[i].critic.Q1(s0_critic_in, pred_a).mean()
                    actor_loss += (curr_pol_out ** 2).mean() * 1e-3

                self.agents[i].actor_optimizer.zero_grad()
                actor_loss.backward()
                torch.nn.utils.clip_grad_norm_(self.agents[i].actor.parameters(), 0.5)
                self.agents[i].actor_optimizer.step()
                total_loss_actor += actor_loss.item()

                if not BC:
                    # ???????????????
                    soft_update(self.agents[i].target_actor, self.agents[i].actor, self.tau)
                    soft_update(self.agents[i].target_critic, self.agents[i].critic, self.tau)

        #????????????bc,??????????????????????????????
        return (total_critic_loss, total_loss_actor)

