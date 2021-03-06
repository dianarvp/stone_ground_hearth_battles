import logging
import random
from typing import List, Optional

import torch
from torch import nn
from torch.distributions import Categorical

from hearthstone.agent import Agent, Action
from hearthstone.training.pytorch.hearthstone_state_encoder import encode_player, encode_valid_actions, State, \
    EncodedActionSet, get_indexed_action, get_action_index, Transition

import torch.nn.functional as F
logger = logging.getLogger(__name__)


class PytorchBot(Agent):
    def __init__(self, net: nn.Module):
        self.authors = []
        self.net = net

    def policy(self, player: 'Player') -> Categorical:
        encoded_state: State = encode_player(player)
        valid_actions_mask: EncodedActionSet = encode_valid_actions(player)
        policy, value = self.net(State(encoded_state.player_tensor.unsqueeze(0),
                                       encoded_state.cards_tensor.unsqueeze(0)),
                                 EncodedActionSet(valid_actions_mask.player_action_tensor.unsqueeze(0),
                                                  valid_actions_mask.card_action_tensor.unsqueeze(0)))
        #print(player.tavern.turn_count, player.health, float(value), sorted([p.health for p in player.tavern.players.values()]), encoded_state.player_tensor)
        #print(player.in_play)
        return policy

    def buy_phase_action(self, player: 'Player') -> Action:
        policy = self.policy(player)
        action = Categorical(torch.exp(policy)).sample()
        return get_indexed_action(int(action))

    #TODO handle learning card and discover choice actions
    def rearrange_cards(self, player: 'Player') -> List['Card']:
        return player.in_play

    def discover_choice_action(self, player: 'Player') -> 'Card':
        return random.choice(player.discovered_cards)

