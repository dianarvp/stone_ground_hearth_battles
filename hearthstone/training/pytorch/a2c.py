import logging
import random
from collections import namedtuple
from typing import List

import torch
from torch import optim, nn

from hearthstone.battlebots.cheapo_bot import CheapoBot
from hearthstone.battlebots.no_action_bot import NoActionBot
from hearthstone.battlebots.random_bot import RandomBot
from hearthstone.battlebots.saurolisk_bot import SauroliskBot
from hearthstone.battlebots.supremacy_bot import SupremacyBot
from hearthstone.host import RoundRobinHost
from hearthstone.ladder.ladder import Contestant, update_ratings, print_standings, save_ratings
from hearthstone.monster_types import MONSTER_TYPES
from hearthstone.training.pytorch.feedforward_net import HearthstoneFFNet
from hearthstone.training.pytorch.hearthstone_state_encoder import Transition, default_player_encoding, \
    default_cards_encoding, EncodedActionSet, get_indexed_action
from hearthstone.training.pytorch.pytorch_bot import PytorchBot
from hearthstone.training.pytorch.replay_buffer import BigBrotherAgent, ReplayBuffer


def easiest_contestants():
    all_bots = [Contestant(f"RandomBot {i}", RandomBot(i)) for i in range(20)]
    all_bots += [Contestant(f"NoActionBot ", NoActionBot())]
    all_bots += [Contestant(f"CheapoBot", CheapoBot(3))]
    return all_bots


def easier_contestants():
    all_bots = [Contestant(f"RandomBot {i}", RandomBot(i)) for i in range(20)]
    all_bots += [Contestant(f"NoActionBot ", NoActionBot())]
    all_bots += [Contestant(f"CheapoBot", CheapoBot(3))]
    all_bots += [Contestant(f"SupremacyBot {t}", SupremacyBot(t, False, i)) for i, t in
                 enumerate([MONSTER_TYPES.MURLOC, MONSTER_TYPES.BEAST, MONSTER_TYPES.MECH, MONSTER_TYPES.DRAGON,
                            MONSTER_TYPES.DEMON, MONSTER_TYPES.PIRATE])]
    all_bots += [Contestant(f"SupremacyUpgradeBot {t}", SupremacyBot(t, True, i)) for i, t in
                 enumerate([MONSTER_TYPES.MURLOC, MONSTER_TYPES.BEAST, MONSTER_TYPES.MECH, MONSTER_TYPES.DRAGON,
                            MONSTER_TYPES.DEMON, MONSTER_TYPES.PIRATE])]
    all_bots += [Contestant("SauroliskBot", SauroliskBot(5))]
    return all_bots

StateBatch = namedtuple('StateBatch', ('player_tensor', 'cards_tensor'))
TransitionBatch = namedtuple('TransitionBatch', ('state', 'valid_actions', 'action', 'next_state', 'reward', 'is_terminal'))


# TODO: Delete all of this
def tensorize_batch(transitions: List[Transition]) -> TransitionBatch:
    player_tensor = torch.stack([transition.state.player_tensor for transition in transitions])
    cards_tensor = torch.stack([transition.state.cards_tensor for transition in transitions])
    valid_player_actions_tensor = torch.stack([transition.valid_actions.player_action_tensor for transition in transitions])
    valid_card_actions_tensor = torch.stack([transition.valid_actions.card_action_tensor for transition in transitions])
    action_tensor = torch.tensor([transition.action for transition in transitions])
    next_player_tensor = torch.stack([transition.next_state.player_tensor for transition in transitions])
    next_cards_tensor = torch.stack([transition.next_state.cards_tensor for transition in transitions])
    reward_tensor = torch.tensor([transition.reward for transition in transitions])
    is_terminal_tensor = torch.tensor([transition.is_terminal for transition in transitions])
    return TransitionBatch(StateBatch(player_tensor, cards_tensor),
                           EncodedActionSet(valid_player_actions_tensor, valid_card_actions_tensor),
                           action_tensor,
                           StateBatch(next_player_tensor, next_cards_tensor),
                           reward_tensor,
                           is_terminal_tensor)


def learn(optimizer: optim.Adam, learning_net: nn.Module, replay_buffer: ReplayBuffer, batch_size, policy_weight):
    if len(replay_buffer) < batch_size:
        return

    transitions: List[Transition] = replay_buffer.sample(batch_size)
    replay_buffer.clear()
    transition_batch = tensorize_batch(transitions)
    # TODO turn off gradient here
    # Note transition_batch.valid_actions is not the set of valid actions from the next state, but we are ignoring the policy network here so it doesn't matter
    next_policy_, next_value = learning_net(transition_batch.next_state, transition_batch.valid_actions)
    next_value = next_value.detach()

    policy, value = learning_net(transition_batch.state, transition_batch.valid_actions)
    advantage = transition_batch.reward.unsqueeze(-1) + next_value.masked_fill(
        transition_batch.is_terminal.unsqueeze(-1), 0.0) - value


    # print("reward", transition_batch.reward)
    # print("value", value)
    #print("next_value", next_value)
    #for i in range(5):
    #     print("action", get_indexed_action(int(transition_batch.action[i])))
    #     print("advantage", advantage[i])
    #     print("value", value[i])
    #     print("next_value", next_value[i])
    #

    #     print("avg_advantage", advantage.mean())
    #     print("advantage.pow(2).mean()", advantage.pow(2).mean())
    print("reward", transition_batch.reward.masked_select(transition_batch.is_terminal).float().mean())
    print("avg_value", value.mean())
    print("avg_advantage", advantage.mean())
    policy_loss = -(policy.gather(1, transition_batch.action.unsqueeze(-1)) * advantage).mean()
    value_loss = advantage.pow(2).mean()
    print("policy_loss", policy_loss)
    print("value_loss", value_loss)
    valid_action_tensor = torch.cat(
        (transition_batch.valid_actions.player_action_tensor.flatten(1), transition_batch.valid_actions.card_action_tensor.flatten(1)), dim=1)
    print("policy", torch.exp(policy.masked_select(valid_action_tensor)).max())
    entropy_loss = torch.sum(policy * torch.exp(policy))
    loss = policy_loss * policy_weight + (1-policy_weight)*value_loss #- 0.000001*entropy_loss
    #loss = value_loss
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()


def main():
    batch_size = 64
    logging.getLogger().setLevel(logging.INFO)
    other_contestants = easier_contestants()
    learning_net = HearthstoneFFNet(default_player_encoding(), default_cards_encoding())
    optimizer = optim.Adam(learning_net.parameters(), lr=0.001)
    learning_bot = PytorchBot(learning_net)
    replay_buffer = ReplayBuffer(100000)
    big_brother = BigBrotherAgent(learning_bot, replay_buffer)
    learning_bot_contestant = Contestant("LearningBot", big_brother)
    contestants = other_contestants + [learning_bot_contestant]
    standings_path = "../../data/learning/pytorch/a2c/standings.json"
    #load_ratings(contestants, standings_path)

    for _ in range(1000):
        round_contestants = [learning_bot_contestant] + random.sample(other_contestants, k=7)
        host = RoundRobinHost({contestant.name: contestant.agent for contestant in round_contestants})
        host.play_game()
        winner_names = list(reversed([name for name, player in host.tavern.losers]))
        print("---------------------------------------------------------------")
        print(winner_names)
        #print(host.tavern.losers[-1][1].in_play)
        ranked_contestants = sorted(round_contestants, key=lambda c: winner_names.index(c.name))
        update_ratings(ranked_contestants)
        print_standings(contestants)
        for contestant in round_contestants:
            contestant.games_played += 1
        if learning_bot_contestant in round_contestants:
            learn(optimizer, learning_net, replay_buffer, batch_size, 0.01)


    save_ratings(contestants, standings_path)


if __name__ == '__main__':
    main()
