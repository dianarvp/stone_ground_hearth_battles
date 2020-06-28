from typing import Optional

SUMMON_BUY = 1
SUMMON_COMBAT = 2
KILL = 3
DIES = 4
COMBAT_START = 5
BUY_START = 6
ON_ATTACK = 7
SELL = 8
BUY = 9

class BuyPhaseContext:
    def __init__(self, owner: 'Player', randomizer: 'Randomizer'):
        self.owner = owner
        self.randomizer = randomizer


class CombatPhaseContext:
    def __init__(self, friendly_war_party: 'WarParty', enemy_war_party: 'WarParty', randomizer: 'Randomizer'):
        self.friendly_war_party = friendly_war_party
        self.enemy_war_party = enemy_war_party
        self.randomizer = randomizer

    def broadcast_combat_event(self, event: 'CardEvent'):
        self.friendly_war_party.owner.hero.handle_event(event, self)
        for card in self.friendly_war_party.board:
            # it's ok for the card to be dead
            card.handle_event(event, self)
        self.enemy_war_party.owner.hero.handle_event(event, self.enemy_context())
        for card in self.enemy_war_party.board:
            card.handle_event(event, self.enemy_context())

    def enemy_context(self):
        return CombatPhaseContext(self.enemy_war_party, self.friendly_war_party, self.randomizer)
