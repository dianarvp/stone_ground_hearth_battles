import copy
import enum
from collections import namedtuple
from typing import Callable, List, Any, Optional, Dict

import torch

from hearthstone.agent import TripleRewardsAction, TavernUpgradeAction, RerollAction, \
    EndPhaseAction, SummonAction, BuyAction, SellFromBoardAction, SellFromHandAction, Action
from hearthstone.cards import Card
from hearthstone.monster_types import MONSTER_TYPES
from hearthstone.player import Player, StoreIndex, HandIndex, BoardIndex

State = namedtuple('State', ('player_tensor', 'cards_tensor'))

Transition = namedtuple('Transition',
                        ('state', 'valid_actions', 'action', 'action_prob', 'next_state', 'reward', 'is_terminal'))


def frozen_player(player: Player) -> Player:
    player = copy.copy(player)
    player.tavern = None
    player = copy.deepcopy(player)
    return player


MAX_ENCODED_STORE = 7
MAX_ENCODED_HAND = 10
MAX_ENCODED_BOARD = 7


class CardLocation(enum.Enum):
    STORE = 1
    HAND = 2
    BOARD = 3


class LocatedCard:
    def __init__(self, card: Card, location: CardLocation):
        self.card = card
        self.location = location


class Feature:

    def fill_tensor(self, obj: Any, view: torch.Tensor):
        pass

    def size(self) -> torch.Size:
        pass

    def dtype(self) -> torch.dtype:
        pass

    def encode(self, obj: Any) -> torch.Tensor:
        tensor = torch.zeros(self.size(), dtype=self.dtype())
        self.fill_tensor(obj, tensor)
        return tensor

    def flattened_size(self) -> int:
        num = 1
        for dim in self.size():
            num *= dim
        return num


class ScalarFeature(Feature):
    def __init__(self, feat: Callable[[Any], Any], dtype=None):
        self._dtype = dtype or torch.float
        self.feat = feat

    def fill_tensor(self, obj: Any, view: torch.Tensor):
        view.data[0] = self.feat(obj)

    def size(self) -> torch.Size:
        return torch.Size([1])

    def dtype(self) -> torch.dtype:
        return self._dtype


class OnehotFeature(Feature):
    def __init__(self, extractor: Callable[[Any], Any], num_classes: int, dtype=None):
        self._dtype = dtype or torch.float
        self.extractor = extractor
        self.num_classes = num_classes

    def fill_tensor(self, obj: Any, view: torch.Tensor):
        view[self.extractor(obj)] = 1.0

    def size(self) -> torch.Size:
        return torch.Size([self.num_classes])

    def dtype(self) -> torch.dtype:
        return self._dtype


class CombinedFeature(Feature):
    def __init__(self, features: List[Feature], dtype=None):
        self.features = features
        self._dtype = dtype or torch.float

    def fill_tensor(self, obj: Any, view: torch.Tensor):
        start = 0
        for feature in self.features:
            size = feature.size()
            feature.fill_tensor(obj, view.narrow(0, start, size[0]))
            start += size[0]

    def size(self) -> torch.Size:
        sizes = [feature.size() for feature in self.features]
        dimension_sum = 0
        for size in sizes:
            assert size[1:] == sizes[0][1:]
            dimension_sum += size[0]
        return (dimension_sum,) + sizes[0][1:]

    def dtype(self) -> torch.dtype:
        return self._dtype


class ListOfFeatures(Feature):
    def __init__(self, extractor: Callable[[Any], List[Any]], feature: Feature, width: int, dtype=None):
        self.extractor = extractor
        self.feature = feature
        self.width = width
        self._dtype = dtype or torch.float

    def fill_tensor(self, obj: Any, view: torch.Tensor):
        values_to_encode = self.extractor(obj)
        assert (len(values_to_encode) <= self.width)
        for i, value in enumerate(values_to_encode):
            self.feature.fill_tensor(value, view.narrow(0, i, 1).squeeze(0))

    def size(self):
        return torch.Size((self.width,) + self.feature.size())

    def dtype(self):
        return self._dtype


class SortedByValueFeature(Feature):
    def __init__(self, extractor: Callable[[Any], List[Any]], width: int, dtype=None):
        self.extractor = extractor
        self.width = width
        self._dtype = dtype or torch.float

    def fill_tensor(self, obj: Any, view: torch.Tensor):
        values_to_encode = self.extractor(obj)
        sorted_values = sorted(values_to_encode)
        assert (len(values_to_encode) <= self.width)
        for i, value in enumerate(sorted_values):
            view[i] = value

    def size(self):
        return torch.Size((self.width,))

    def dtype(self):
        return self._dtype


def enum_to_int(value: Optional[enum.Enum]) -> int:
    if value is not None:
        return value.value
    else:
        return 0


def default_card_encoding() -> Feature:
    """
    Default encoder for type `LocatedCard`.
    """
    return CombinedFeature([
        ScalarFeature(lambda card: 1.0),  # Present
        ScalarFeature(lambda card: float(card.card.tier)),
        ScalarFeature(lambda card: float(card.card.attack)),
        ScalarFeature(lambda card: float(card.card.health)),
        ScalarFeature(lambda card: float(card.card.taunt)),
        ScalarFeature(lambda card: float(card.card.divine_shield)),
        OnehotFeature(lambda card: enum_to_int(card.card.monster_type), len(MONSTER_TYPES) + 1),
        OnehotFeature(lambda card: enum_to_int(card.location), len(CardLocation) + 1),
    ])


def default_player_encoding() -> Feature:
    """
    Default encoder for the player level features (non-card features).

    Encodes a `Player`.
    """

    return CombinedFeature([
        ScalarFeature(lambda player: float(player.tavern.turn_count)),
        ScalarFeature(lambda player: float(player.health)),
        ScalarFeature(lambda player: float(player.coins)),
        SortedByValueFeature(lambda player: [p.health for name, p in player.tavern.players.items()], 8),
    ])



def default_cards_encoding() -> Feature:
    """
    Default encoder for the card-level features.

    Encodes a `Player`.
    """
    return CombinedFeature([
        ListOfFeatures(
            lambda player: [LocatedCard(card, CardLocation.STORE) for card in player.store],
            default_card_encoding(), MAX_ENCODED_STORE),
        ListOfFeatures(
            lambda player: [LocatedCard(card, CardLocation.HAND) for card in player.hand],
            default_card_encoding(), MAX_ENCODED_HAND),
        ListOfFeatures(
            lambda player: [LocatedCard(card, CardLocation.BOARD) for card in player.in_play],
            default_card_encoding(), MAX_ENCODED_BOARD)
    ])


DEFAULT_PLAYER_ENCODING = default_player_encoding()
DEFAULT_CARDS_ENCODING = default_cards_encoding()


def encode_player(player: Player) -> State:
    player_tensor = DEFAULT_PLAYER_ENCODING.encode(player)
    cards_tensor = DEFAULT_CARDS_ENCODING.encode(player)
    return State(player_tensor, cards_tensor)


EncodedActionSet = namedtuple('EncodedActionSet', ('player_action_tensor', 'card_action_tensor'))

ActionSet = namedtuple('ActionSet', ('player_action_set', 'card_action_set'))


class InvalidAction(Action):
    def __repr__(self):
        return f"InvalidAction()"

    def apply(self, player: 'Player'):
        assert False

    def valid(self, player: 'Player') -> bool:
        return False


def store_indices() -> List[StoreIndex]:
    return [StoreIndex(i) for i in range(MAX_ENCODED_STORE)]


def hand_indices() -> List[HandIndex]:
    return [HandIndex(i) for i in range(MAX_ENCODED_HAND)]


def board_indices() -> List[BoardIndex]:
    return [BoardIndex(i) for i in range(MAX_ENCODED_BOARD)]


def _all_actions() -> ActionSet:
    player_action_set = [TripleRewardsAction(), TavernUpgradeAction(), RerollAction(), EndPhaseAction(False),
                         EndPhaseAction(True)]
    store_action_set = [[BuyAction(index), InvalidAction(), InvalidAction(), InvalidAction()] for index in store_indices()]
    hand_action_set = [[InvalidAction(), SummonAction(index), SummonAction(index, [BoardIndex(0)]), SellFromHandAction(index)] for index in
                       hand_indices()]
    board_action_set = [[InvalidAction(), InvalidAction(), InvalidAction(), SellFromBoardAction(index)] for index in
                        board_indices()]
    return ActionSet(player_action_set, store_action_set + hand_action_set + board_action_set)


ALL_ACTIONS = _all_actions()


def _all_actions_dict():
    result = {}
    index = 0
    for player_action in ALL_ACTIONS.player_action_set:
        result[str(player_action)] = index
        index += 1
    for card_actions in ALL_ACTIONS.card_action_set:
        for card_action in card_actions:
            result[str(card_action)] = index
            index += 1
    return result


ALL_ACTIONS_DICT: Dict[str, int] = _all_actions_dict()


def encode_valid_actions(player: Player) -> EncodedActionSet:
    actions = ALL_ACTIONS
    player_action_tensor = torch.tensor([action.valid(player) for action in actions.player_action_set])
    cards_action_tensor = torch.tensor(
        [[action.valid(player) for action in card_actions] for card_actions in actions.card_action_set])
    return EncodedActionSet(player_action_tensor, cards_action_tensor)


def action_encoding_size() -> int:
    player_action_size = len(ALL_ACTIONS.player_action_set)
    card_action_size = len(ALL_ACTIONS.card_action_set) * len(ALL_ACTIONS.card_action_set[0])
    return player_action_size + card_action_size


def get_action_index(action: Action) -> int:
    return ALL_ACTIONS_DICT[str(action)]


def get_indexed_action(index: int) -> Action:

    if index < len(ALL_ACTIONS.player_action_set):
        return ALL_ACTIONS.player_action_set[index]
    else:
        card_action_index = index - len(ALL_ACTIONS.player_action_set)
        card_index = card_action_index // len(ALL_ACTIONS.card_action_set[0])
        within_card_index = card_action_index % len(ALL_ACTIONS.card_action_set[0])
        return ALL_ACTIONS.card_action_set[card_index][within_card_index]
