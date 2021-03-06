import logging
import random
import time
from datetime import datetime
from typing import List, Dict

import torch
import trueskill
from torch import optim, nn
from torch.utils.tensorboard import SummaryWriter

from hearthstone.host import RoundRobinHost
from hearthstone.ladder.ladder import Contestant, update_ratings, load_ratings, print_standings
from hearthstone.training.pytorch.feedforward_net import HearthstoneFFNet
from hearthstone.training.pytorch.hearthstone_state_encoder import Transition, get_indexed_action, \
    DEFAULT_PLAYER_ENCODING, DEFAULT_CARDS_ENCODING
from hearthstone.training.pytorch.policy_gradient import tensorize_batch, easy_contestants
from hearthstone.training.pytorch.replay_buffer import ReplayBuffer, SurveiledPytorchBot, NormalizingReplayBuffer


class Worker:
    def __init__(self, learning_bot_contestant: Contestant, other_contestants: List[Contestant]):
        self.other_contestants = other_contestants
        self.learning_bot_contestant = learning_bot_contestant
        self.host = None
        self.round_contestants = None
        self.one_step_generator = None
        self.learning_bot_agent = None
        self._start_new_game()

    def _start_new_game(self):
        self.round_contestants = [self.learning_bot_contestant] + random.sample(self.other_contestants, k=7)
        self.host = RoundRobinHost( {contestant.name: contestant.agent_generator() for contestant in self.round_contestants})
        self.learning_bot_agent = self.host.agents[self.learning_bot_contestant.name]
        self.host.start_game()
        self.one_step_generator = self.host.play_round_generator()

    def play_round(self):
        last_replay_buffer_position = self.learning_bot_agent.replay_buffer.position
        while self.learning_bot_agent.replay_buffer.position == last_replay_buffer_position:
            try:
               next(self.one_step_generator)
            except StopIteration as e:
                if self.host.game_over():
                    winner_names = list(reversed([name for name, player in self.host.tavern.losers]))
                    # print("---------------------------------------------------------------")
                    # print(winner_names)
                    # print(self.host.tavern.players[self.learning_bot_contestant.name].in_play)
                    ranked_contestants = sorted(self.round_contestants, key=lambda c: winner_names.index(c.name))
                    update_ratings(ranked_contestants)
                    # print_standings([self.learning_bot_contestant] + self.other_contestants)
                    for contestant in self.round_contestants:
                        contestant.games_played += 1

                    self._start_new_game()
                else:
                    self.one_step_generator = self.host.play_round_generator()



# TODO STOP THIS HACK
expensive_tensorboard = False
# Some things crash under optuna.
enable_crashing_tensorboard = False
def learn(tensorboard: SummaryWriter, optimizer: optim.Optimizer, learning_net: nn.Module, replay_buffer: ReplayBuffer, batch_size, policy_weight, entropy_weight, ppo_epsilon, gradient_clipping, normalize_advantage, global_step):
    global expensive_tensorboard
    transitions: List[Transition] = replay_buffer.sample(batch_size)
    transition_batch = tensorize_batch(transitions)
    # TODO turn off gradient here
    # Note transition_batch.valid_actions is not the set of valid actions from the next state, but we are ignoring the policy network here so it doesn't matter
    next_policy_, next_value = learning_net(transition_batch.next_state, transition_batch.valid_actions)
    next_value = next_value.detach()

    policy, value = learning_net(transition_batch.state, transition_batch.valid_actions)
    value_target = transition_batch.reward.unsqueeze(-1) + next_value.masked_fill(
        transition_batch.is_terminal.unsqueeze(-1), 0.0)
    advantage = value_target - value
    clipped_advantage = value_target - next_value + torch.clamp(next_value - value, -ppo_epsilon, ppo_epsilon)

    masked_reward = transition_batch.reward.masked_select(transition_batch.is_terminal)


    ratio = torch.exp(policy - transition_batch.action_prob.unsqueeze(-1)).gather(1, transition_batch.action.unsqueeze(-1))
    clipped_ratio = ratio.clamp(1 - ppo_epsilon, 1 + ppo_epsilon)

    normalized_advantage: torch.Tensor = advantage.detach()
    if normalize_advantage:
        normalized_advantage = (normalized_advantage - normalized_advantage.mean()) / (normalized_advantage.std()+1e-5)
    clipped_policy_loss = - clipped_ratio * normalized_advantage
    unclipped_policy_loss = - ratio * normalized_advantage
    policy_loss = torch.max(clipped_policy_loss, unclipped_policy_loss).mean()
    value_loss = torch.max(advantage.pow(2), clipped_advantage.pow(2)).mean()
    entropy_loss = entropy_weight * torch.sum(policy * torch.exp(policy))

    #Not actually expensive
    if enable_crashing_tensorboard:
        tensorboard.add_histogram("policy/train", torch.exp(policy), global_step)
        if masked_reward.size()[0]:
            tensorboard.add_histogram("reward/train", transition_batch.reward.masked_select(transition_batch.is_terminal),
                                  global_step)
        tensorboard.add_histogram("value/train", value, global_step)
        tensorboard.add_histogram("next_value/train", next_value, global_step)
        tensorboard.add_histogram("advantage/train", advantage, global_step)
        tensorboard.add_histogram("unclipped_policy_loss/train", unclipped_loss, global_step)
        tensorboard.add_histogram("clipped_policy_loss/train", clipped_loss, global_step)
        tensorboard.add_histogram("policy_ratio/train", ratio, global_step)
    tensorboard.add_text("action/train", str(get_indexed_action(int(transition_batch.action[0]))), global_step)
    tensorboard.add_scalar("avg_reward/train", transition_batch.reward.masked_select(transition_batch.is_terminal).float().mean(), global_step)
    tensorboard.add_scalar("avg_value/train", value.mean(), global_step)
    tensorboard.add_scalar("avg_advantage/train", advantage.mean(), global_step)
    tensorboard.add_scalar("policy_loss/train", policy_loss, global_step)
    tensorboard.add_scalar("value_loss/train", value_loss, global_step)
    tensorboard.add_scalar("avg_unclipped_policy_loss/train", unclipped_policy_loss.mean(), global_step)
    tensorboard.add_scalar("avg_clipped_policy_loss/train", clipped_policy_loss.mean(), global_step)

    tensorboard.add_scalar("entropy_loss/train", entropy_loss, global_step)
    loss = policy_loss * policy_weight + value_loss + entropy_loss

    optimizer.zero_grad()
    loss.backward()
    if gradient_clipping:
        torch.nn.utils.clip_grad_norm_(learning_net.parameters(), gradient_clipping)
    optimizer.step()
    if expensive_tensorboard:
        for tag, parm in learning_net.named_parameters():
            tensorboard.add_histogram(f"gradients_{tag}/train", parm.grad.data, global_step)


def ppo(hparams: Dict, time_limit_secs=None, early_stopper= None):
    start_time = time.time()
    last_reported_time = start_time
    batch_size = hparams['batch_size']

    tensorboard = SummaryWriter(f"../../../data/learning/pytorch/tensorboard/{datetime.now().isoformat()}")
    logging.getLogger().setLevel(logging.INFO)
    learning_net = HearthstoneFFNet(DEFAULT_PLAYER_ENCODING, DEFAULT_CARDS_ENCODING, hparams["nn_hidden_layers"],
                                    hparams.get("nn_hidden_size") or 0,
                                    hparams.get("nn_shared") or False,
                                    hparams.get("nn_activation") or "")
    if hparams["optimizer"] == "adam":
        optimizer = optim.Adam(learning_net.parameters(), lr=hparams["adam_lr"])
    elif hparams["optimizer"] == "sgd":
        optimizer = optim.SGD(learning_net.parameters(), lr=hparams["sgd_lr"], momentum=hparams["sgd_momentum"], nesterov=True)
    else:
        assert False
    global_step = 0
    replay_buffer_size = 10000
    if hparams["normalize_observations"]:
        replay_buffer = NormalizingReplayBuffer(replay_buffer_size, 0.99, DEFAULT_PLAYER_ENCODING, DEFAULT_CARDS_ENCODING)
    else:
        replay_buffer = ReplayBuffer(replay_buffer_size)
    learning_bot_contestant = Contestant("LearningBot", lambda: SurveiledPytorchBot(learning_net, replay_buffer))
    learning_bot_contestant.trueskill = trueskill.Rating(14)
    # Reuse standings from the current leaderboard.
    other_contestants = easy_contestants()
    load_ratings(other_contestants, "../../../data/standings.json")

    workers = [Worker(learning_bot_contestant, other_contestants) for _ in range(hparams['num_workers'])]

    for _ in range(1000000):
        for worker in workers:
            worker.play_round()
        # print(len(replay_buffer))
        if len(replay_buffer) >= batch_size:
            for i in range(hparams["ppo_epochs"]):
                learn(tensorboard, optimizer, learning_net, replay_buffer, batch_size, hparams["policy_weight"],
                      hparams["entropy_weight"], hparams["ppo_epsilon"], hparams["gradient_clipping"], hparams["normalize_advantage"],
                      global_step)
                global_step += 1
            replay_buffer.clear()
        time_elapsed = int(time.time() - start_time)
        tensorboard.add_scalar("elo/train", learning_bot_contestant.elo, global_step=global_step)
        tensorboard.add_scalar("trueskill_mu/train", learning_bot_contestant.trueskill.mu, global_step=global_step)
        if early_stopper:
            if time.time() - last_reported_time > 5:
                last_reported_time = time.time()
                early_stopper.report(learning_bot_contestant.trueskill.mu, time_elapsed)
            if early_stopper.should_prune():
                break
        if time_limit_secs and time_elapsed > time_limit_secs:
            break

    tensorboard.add_hparams(hparam_dict=hparams, metric_dict={"optuna_trueskill": learning_bot_contestant.trueskill.mu})
    tensorboard.close()
    return learning_bot_contestant.trueskill.mu


def main():
    ppo({'adam_lr': 0.000698178899316577,
         'batch_size': 269,
         'entropy_weight': 3.20049705838473e-05,
         'gradient_clipping': 0.5,
         'nn_hidden_layers': 0,
         'normalize_advantage': True,
         'normalize_observations': False,
         'num_workers': 1,
         'optimizer': 'adam',
         'policy_weight': 0.581166675499831,
         'ppo_epochs': 8,
         'ppo_epsilon': 0.160450364515127})


if __name__ == '__main__':
    main()
