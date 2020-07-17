import logging
import random
from datetime import datetime
from typing import List

import torch
from torch import optim, nn
from torch.utils.tensorboard import SummaryWriter

from hearthstone.host import RoundRobinHost
from hearthstone.ladder.ladder import Contestant, update_ratings, print_standings, save_ratings
from hearthstone.training.pytorch.feedforward_net import HearthstoneFFNet
from hearthstone.training.pytorch.hearthstone_state_encoder import Transition, get_indexed_action, \
    DEFAULT_PLAYER_ENCODING, DEFAULT_CARDS_ENCODING
from hearthstone.training.pytorch.policy_gradient import easier_contestants, tensorize_batch
from hearthstone.training.pytorch.pytorch_bot import PytorchBot
from hearthstone.training.pytorch.replay_buffer import ReplayBuffer, SurveiledPytorchBot

# TODO STOP THIS HACK
global_step = 0

def learn(tensorboard: SummaryWriter, optimizer: optim.Adam, learning_net: nn.Module, replay_buffer: ReplayBuffer, batch_size, policy_weight):
    global global_step
    transitions: List[Transition] = replay_buffer.sample(batch_size)
    transition_batch = tensorize_batch(transitions)
    # TODO turn off gradient here
    # Note transition_batch.valid_actions is not the set of valid actions from the next state, but we are ignoring the policy network here so it doesn't matter
    next_policy_, next_value = learning_net(transition_batch.next_state, transition_batch.valid_actions)
    next_value = next_value.detach()

    policy, value = learning_net(transition_batch.state, transition_batch.valid_actions)
    advantage = transition_batch.reward.unsqueeze(-1) + next_value.masked_fill(
        transition_batch.is_terminal.unsqueeze(-1), 0.0) - value

    tensorboard.add_histogram("policy/train", torch.exp(policy), global_step)
    masked_reward = transition_batch.reward.masked_select(transition_batch.is_terminal)
    if masked_reward.size()[0]:
        tensorboard.add_histogram("reward/train", transition_batch.reward.masked_select(transition_batch.is_terminal), global_step)
    tensorboard.add_histogram("value/train", value, global_step)
    tensorboard.add_histogram("next_value/train", next_value, global_step)
    tensorboard.add_histogram("advantage/train", advantage, global_step)
    tensorboard.add_text("action/train", str(get_indexed_action(int(transition_batch.action[0]))), global_step)
    tensorboard.add_scalar("avg_reward/train", transition_batch.reward.masked_select(transition_batch.is_terminal).float().mean(), global_step)
    tensorboard.add_scalar("avg_value/train", value.mean(), global_step)
    tensorboard.add_scalar("avg_advantage/train", advantage.mean(), global_step)
    ratio = torch.exp(policy - transition_batch.action_prob.unsqueeze(-1)).gather(1, transition_batch.action.unsqueeze(-1))
    epsilon = 0.2
    clipped_ratio = ratio.clamp(1-epsilon, 1+epsilon)
    unclipped_loss = - ratio * advantage.detach()
    clipped_loss = - clipped_ratio * advantage.detach()
    policy_loss = torch.max(unclipped_loss, clipped_loss).mean()
    value_loss = advantage.pow(2).mean()
    tensorboard.add_scalar("policy_loss/train", policy_loss, global_step)
    tensorboard.add_scalar("value_loss/train", value_loss, global_step)
    tensorboard.add_scalar("avg_unclipped_policy_loss/train", unclipped_loss.mean(), global_step)
    tensorboard.add_scalar("avg_clipped_policy_loss/train", clipped_loss.mean(), global_step)
    tensorboard.add_histogram("unclipped_policy_loss/train", unclipped_loss, global_step)
    tensorboard.add_histogram("clipped_policy_loss/train", clipped_loss, global_step)
    tensorboard.add_histogram("policy_ratio/train", ratio, global_step)
    entropy_loss = - 0.000001 * torch.sum(policy * torch.exp(policy))
    tensorboard.add_scalar("entropy_loss/train", entropy_loss, global_step)
    loss = policy_loss * policy_weight + value_loss + entropy_loss

    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    global_step += 1


def main():
    batch_size = 512
    num_workers = 6

    tensorboard = SummaryWriter(f"../../../data/learning/pytorch/tensorboard/{datetime.now().isoformat()}")
    logging.getLogger().setLevel(logging.INFO)
    other_contestants = easier_contestants()
    learning_net = HearthstoneFFNet(DEFAULT_PLAYER_ENCODING, DEFAULT_CARDS_ENCODING)
    optimizer = optim.Adam(learning_net.parameters(), lr=0.0003)
    replay_buffer = ReplayBuffer(10000)
    learning_bot = SurveiledPytorchBot(learning_net, replay_buffer)
    learning_bot_contestant = Contestant("LearningBot", learning_bot)
    contestants = other_contestants + [learning_bot_contestant]
    standings_path = "../../../data/learning/pytorch/a2c/standings.json"
    #load_ratings(contestants, standings_path)
    #add_net_to_tensorboard(tensorboard, learning_net)

    tensorboard.add_text("learning_algorithm", "PPO")
    for _ in range(100000):
        round_contestants = [learning_bot_contestant] + random.sample(other_contestants, k=7)
        host = RoundRobinHost({contestant.name: contestant.agent for contestant in round_contestants})
        host.play_game()
        winner_names = list(reversed([name for name, player in host.tavern.losers]))
        print("---------------------------------------------------------------")
        print(winner_names)
        print(host.tavern.players[learning_bot_contestant.name].in_play)
        ranked_contestants = sorted(round_contestants, key=lambda c: winner_names.index(c.name))
        update_ratings(ranked_contestants)
        print_standings(contestants)
        for contestant in round_contestants:
            contestant.games_played += 1
        if learning_bot_contestant in round_contestants:
            if len(replay_buffer) >= batch_size:
                for i in range(20):
                    learn(tensorboard, optimizer, learning_net, replay_buffer, batch_size, 2.0)
                replay_buffer.clear()

    save_ratings(contestants, standings_path)
    tensorboard.close()


if __name__ == '__main__':
    main()
