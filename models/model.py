import copy
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import utility.utils as utils
from math import floor

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Implementation of Twin Delayed Deep Deterministic Policy Gradients (TD3)
# Paper: https://arxiv.org/abs/1802.09477
# Original Implementation found on https://github.com/sfujim/TD3/blob/master/TD3.py
# Regular DDPG: https://github.com/ghliu/pytorch-ddpg

class FirstBlock(nn.Module):
    def __init__(self, in_channel, out_channel):
        super().__init__()
        self.conv = nn.Conv2d(in_channel, out_channel, kernel_size=3, stride=1, padding=1, bias=False)
        torch.nn.init.kaiming_normal_(self.conv.weight, mode='fan_in')
        self.bn = nn.BatchNorm2d(out_channel)
        self.prelu = nn.PReLU()
        
    def forward(self, x):
        out = self.conv(x)
        out = self.bn(out)
        out = self.prelu(out)
        return out    

    
class InnerBlock(nn.Module):
    def __init__(self, in_channel, out_channel, stride=1):
        super().__init__()
        assert stride == 1 or stride == 2
        self.conv = nn.Conv2d(in_channel, out_channel, kernel_size=3, stride=stride, padding=1)
        torch.nn.init.kaiming_normal_(self.conv.weight, mode='fan_out')
        self.bn = nn.BatchNorm2d(out_channel)
        
        self.conv2 = nn.Conv2d(out_channel, out_channel, kernel_size=3, stride=1, padding=1)
        torch.nn.init.kaiming_normal_(self.conv2.weight, mode='fan_out')
        self.bn2 = nn.BatchNorm2d(out_channel)
        
        self.prelu = nn.PReLU()
        
        if stride == 1 and in_channel == out_channel:
            self.shortcut = nn.Identity()
        else:            
            self.shortcut = nn.Conv2d(in_channel, out_channel, kernel_size=1, stride=stride)
            torch.nn.init.kaiming_normal_(self.shortcut.weight, mode='fan_out')

            
    def forward(self, x):
        out = self.conv(x)
        out = self.bn(out)
        out = self.prelu(out)
        
        out = self.conv2(out)
        out = self.bn2(out)
        out = self.prelu(self.shortcut(x) + out)
        return out   


class CNN(nn.Module):
    def __init__(
        self,
        indicator_state_dim,
        immediate_state_dim,
        outchannel,
        activation=nn.PReLU,
    ):
        super(CNN, self).__init__()
        self.layers = nn.Sequential(
            FirstBlock(1, 32),

            InnerBlock(32, 32), 
            InnerBlock(32, 32),

            InnerBlock(32, 32, stride=2), 
            InnerBlock(32, 32),
            nn.Dropout(0.15),
            InnerBlock(32, 32),

            InnerBlock(32, 32, stride=2), 
            InnerBlock(32, 32), 
            nn.Dropout(0.15),
            InnerBlock(32, 32), 

            InnerBlock(32, 64, stride=2), 
            InnerBlock(64, 64), 
            nn.Dropout(0.15),
            InnerBlock(64, 64), 
            nn.AdaptiveAvgPool2d((1, 1))
        )
       
        self.layers2 = nn.Sequential(
            FirstBlock(1, 32),

            InnerBlock(32, 32), 
            InnerBlock(32, 32),

            InnerBlock(32, 32, stride=2), 
            InnerBlock(32, 32),
            nn.Dropout(0.15),
            InnerBlock(32, 32),

            InnerBlock(32, 32, stride=2), 
            InnerBlock(32, 32), 
            nn.Dropout(0.15),
            InnerBlock(32, 32), 

            InnerBlock(32, 64, stride=2), 
            InnerBlock(64, 64), 
            nn.Dropout(0.15),
            InnerBlock(64, 64), 

            nn.AdaptiveAvgPool2d((1, 1))
        )       

        self.flatten = nn.Flatten()
        self.output = nn.Linear(64 + 64,  outchannel)


    def forward(self, X, X_immediate):
        out = self.layers(X.unsqueeze(1))   
        out = self.flatten(out)

        out2 = self.layers2(X_immediate.unsqueeze(1))
        out2 = self.flatten(out2)

        out = self.output(torch.cat((out, out2), -1))
        return out


class Actor(nn.Module):
    def __init__(self, state_dim, action_dim, max_action):
        super(Actor, self).__init__()
        self.l1 = nn.Linear(state_dim, 400)
        self.l2 = nn.Linear(400, 300)
        self.l3 = nn.Linear(300, action_dim)

        self.prelu1 = nn.PReLU()
        self.prelu2 = nn.PReLU()
        self.max_action = max_action
        self.init_weights()
    
    def init_weights(self):
        layers = [self.l1, self.l2, self.l3]
        for layer in layers:
            torch.nn.init.kaiming_uniform_(layer.weight)
    def forward(self, state):
        a = self.prelu1(self.l1(state))
        a = self.prelu2(self.l2(a))
        a = self.max_action * torch.tanh(self.l3(a))
        return a

class Critic(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Critic, self).__init__()
        # Q1 architecture
        self.l1 = nn.Linear(state_dim + action_dim, 400)
        self.prelu1 = nn.PReLU()
        self.l2 = nn.Linear(400, 300)
        self.prelu2 = nn.PReLU()
        self.l3 = nn.Linear(300, 1)
        self.init_weights()
    
    def init_weights(self):
        layers = [self.l1, self.l2, self.l3]
        for layer in layers:
            torch.nn.init.kaiming_uniform_(layer.weight)
    


    def forward(self, state, action):
        sa = torch.cat([state, action], 1)
        q = self.prelu1(self.l1(sa))
        q = self.prelu2(self.l2(q))
        q = self.l3(q)
        return q

    def Q1(self, state, action):
        sa = torch.cat([state, action], 1)
        q1 = self.prelu1(self.l1(sa))
        q1 = self.prelu2(self.l2(q1))
        q1 = self.l3(q1)
        return q1


class DDPG(object):
    def __init__(
        self,
        state_dim,
        action_dim,
        max_action,
        discount=0.95,
        tau=0.005,
        policy_noise=0.2,
        noise_clip=0.5,
        policy_freq=2,
        lr=3e-4,
    ):
        self.actor = Actor(state_dim, action_dim, max_action).to(device)
        self.actor_target = copy.deepcopy(self.actor)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=lr)
        self.critic = Critic(state_dim, action_dim).to(device)
        self.critic_target = copy.deepcopy(self.critic)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=lr)
        self.max_action = max_action
        self.discount = discount
        self.tau = tau
        self.policy_noise = policy_noise
        self.noise_clip = noise_clip
        self.policy_freq = policy_freq
        self.total_it = 0

        self.actor_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.actor_optimizer, factor=0.5, patience=20,  verbose=True)
        self.critic_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.critic_optimizer, factor=0.5, patience=20,  verbose=True)

    def select_action(self, state):
        state = torch.FloatTensor(state).to(device)
        action = self.actor(state).cpu().data.numpy()
        return action

    def train(self, replay_buffer, batch_size=100):
        self.total_it += 1
        # Sample replay buffer
        (
            state,
            action,
            next_state,
            reward,
            not_done,
        ) = replay_buffer.sample(batch_size)

        with torch.no_grad():
            # Select action according to policy
            next_action = self.actor_target(state) 
          
            # Compute the target Q value
            target_Q  = self.critic_target(
                next_state, next_action
            )
            target_Q = reward + not_done * self.discount * target_Q
        # Get current Q estimates
        current_Q = self.critic(state, action)
        # Compute critic loss
        critic_loss = F.mse_loss(current_Q, target_Q) 

        # Optimize the critic
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()

        # Compute actor losse
        actor_loss = -self.critic.Q1(
            state, self.actor(state),
        ).mean()
        # Optimize the actor
        self.actor_optimizer.zero_grad()

        actor_loss.backward()

        self.actor_optimizer.step()
        # self.actor_scheduler.step(actor_loss)
        # Update the frozen target models
        for param, target_param in zip(
            self.critic.parameters(), self.critic_target.parameters()
        ):
            target_param.data.copy_(
                self.tau * param.data + (1 - self.tau) * target_param.data
            )
        for param, target_param in zip(
            self.actor.parameters(), self.actor_target.parameters()
        ):
            target_param.data.copy_(
                self.tau * param.data + (1 - self.tau) * target_param.data
            )
    

    def save(self, filename):
        torch.save(self.critic.state_dict(), filename + "_critic")
        torch.save(self.critic_optimizer.state_dict(), filename + "_critic_optimizer")
        torch.save(self.actor.state_dict(), filename + "_actor")
        torch.save(self.actor_optimizer.state_dict(), filename + "_actor_optimizer")

    def load(self, filename):
        self.critic.load_state_dict(torch.load(filename + "_critic", map_location=torch.device('cpu')))
        self.critic_optimizer.load_state_dict(
            torch.load(filename + "_critic_optimizer", map_location=torch.device('cpu'))
        )
        self.critic_target = copy.deepcopy(self.critic)
        self.actor.load_state_dict(torch.load(filename + "_actor", map_location=torch.device('cpu')))
        self.actor_optimizer.load_state_dict(torch.load(filename + "_actor_optimizer", map_location=torch.device('cpu')))
        self.actor_target = copy.deepcopy(self.actor)


class ReplayBuffer(object):
    def __init__(self, state_dim, action_dim, max_size=int(1e6)):
        self.max_size = max_size
        self.ptr = 0
        self.size = 0   
        self.action = np.zeros((max_size, action_dim))
        self.state = np.zeros((max_size, state_dim))
        self.next_state = np.zeros((max_size, state_dim))
        self.reward = np.zeros((max_size, 1))
        self.not_done = np.zeros((max_size, 1))
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def add(self, state, action, next_state, reward, done):
        self.next_state[self.ptr] = next_state
        self.action[self.ptr] = action
        self.state[self.ptr] = state
        self.reward[self.ptr] = reward
        self.not_done[self.ptr] = 1.0 - done
        self.ptr = (self.ptr + 1) % self.max_size
        self.size = min(self.size + 1, self.max_size)

    def sample(self, batch_size):
        ind = np.random.randint(0, self.size, size=batch_size)
        return (
            torch.FloatTensor(self.state[ind]).to(self.device),
            torch.FloatTensor(self.action[ind]).to(self.device),
            torch.FloatTensor(self.next_state[ind]).to(self.device),
            torch.FloatTensor(self.reward[ind]).to(self.device),
            torch.FloatTensor(self.not_done[ind]).to(self.device),
        )

