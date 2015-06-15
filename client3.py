import gtrutils
import card_manager 
from gtrutils import get_detailed_card_summary as det
import gtr
from building import Building
from gamestate import GameState
import message
from fsm import StateMachine

import collections
import logging
import pickle
import itertools
from itertools import izip_longest
from itertools import compress
import time
import glob
import copy

from twisted.python.failure import Failure
from twisted.internet.defer import Deferred

lg = logging.getLogger('gtr')

class StartOverException(Exception):
  pass

class CancelDialogException(Exception):
  pass

class InvalidChoiceException(Exception):
  pass

class LaborerActionBuilder(object):
  """ Puts together the information needed for an architect action,
  since we need multiple inputs from the user.
  """
  def __init__(self, pool, hand, has_dock=False):

    self.fsm = StateMachine()

    self.fsm.add_state('START', None, self.start_transition)
    self.fsm.add_state('FROM_POOL',
        self.from_pool_arrival, self.from_pool_transition)
    self.fsm.add_state('FROM_HAND',
        self.from_hand_arrival, self.from_hand_transition)
    self.fsm.add_state('FINISHED', self.finished_arrival, None, True)

    self.fsm.set_start('START')

    self.hand_cards = sorted(hand, card_manager.cmp_jacks_first)
    self.pool_cards = sorted(pool, card_manager.cmp_jacks_first)

    self.has_dock = has_dock

    self.pool_card = None
    self.hand_card = None

    self.choices = None
    self.prompt = None
    self.done = False
    self.action = None

    # Move from Start state
    self.fsm.pump(None)


  def start_transition(self, _):
    return 'FROM_POOL'


  def from_pool_arrival(self):
    self.choices = [(det(c),True) for c in self.pool_cards]
    self.choices.append(('Skip card from pool', True))
    self.prompt = 'Performing Laborer. Select card from pool'


  def from_pool_transition(self, choice):
    if choice >= 0 and choice < len(self.pool_cards):
      self.pool_card = self.pool_cards[choice]

    elif choice == len(self.pool_cards):
      pass

    else:
      raise InvalidChoiceException()

    return 'FROM_HAND' if self.has_dock else 'FINISHED'


  def from_hand_arrival(self):
    self.choices = [(det(c),not c=='Jack') for c in self.hand_cards]
    self.choices.append(('Skip card from hand', True))
    self.prompt = 'Performing Laborer. Select card from hand'


  def from_hand_transition(self, choice):
    choices = [c for c in self.hand_cards if c != 'Jack']
    if choice >= 0 and choice < len(choices):
      self.hand_card = choices[choice]

    elif choice == len(choices):
      pass

    else:
      raise InvalidChoiceException()

    return 'FINISHED'


  def finished_arrival(self):
    self.done = True
    self.action = message.GameAction(message.LABORER, self.hand_card, self.pool_card)
  

  def get_choices(self):
    return self.choices


  def make_choice(self, choice):
    self.fsm.pump(choice)



if 0:
  def show_choices(self, choices_list, prompt=None):
    """ Returns the index in the choices_list selected by the user or
    raises a StartOverException or a CancelDialogExeption.
    
    The choices_list is either a list of choices, or a list of
    tuples of (choice, selectable) - eg. ('Rubble', False) - such
    that a choice can be listed but unselectable.
    """
    i_choice = 1
    for e in choices_list:
      try:
        choice = e[0]
        selectable = e[1]
      except TypeError:
        choice = e
        selectable = True

      if selectable:
        print '  [{0:2d}] {1}'.format(i_choice, choice)
        i_choice+=1
      else:
        print '       {0}'.format(choice)

    if prompt is not None:
      print prompt
    else:
      print 'Please make a selection:'

  def test_it(self):
    while True:
      self.show_choices(self.get_choices(), self.prompt)
      choice = int(raw_input()) -1
      self.make_choice(choice)
      if self.done:
        print 'Finished! Action: ' + repr(self.action)
        break


class LeadRoleActionBuilder(object):
  """ Builds up a message.LeadRoleAction.
  """
  def __init__(self, hand, has_palace=False, petition_count=3):

    self.fsm = StateMachine()

    self.fsm.add_state('START', None, self.start_transition)
    self.fsm.add_state('SELECT_CARD',
        self.select_card_arrival, self.select_card_transition)
    self.fsm.add_state('PALACE_CARDS',
        self.palace_cards_arrival, self.palace_cards_transition)
    self.fsm.add_state('FIRST_PETITION',
        self.first_petition_arrival, self.first_petition_transition)
    self.fsm.add_state('MORE_PETITIONS',
        self.more_petitions_arrival, self.more_petitions_transition)
    self.fsm.add_state('JACK_ROLE',
        self.jack_role_arrival, self.jack_role_transition)
    self.fsm.add_state('PETITION_ROLE',
        self.petition_role_arrival, self.petition_role_transition)
    self.fsm.add_state('FINISHED', self.finished_arrival, None, True)

    self.fsm.set_start('START')

    self.hand_cards = [(c, True) for c in sorted(hand, card_manager.cmp_jacks_first)]

    self.role = None
    self.choices = None
    
    self.action_units = [] # list of lists: [card1, card2, ... ]
    self.petition_cards = []

    self.has_palace = has_palace
    self.petition_count = petition_count

    self.done = False
    self.prompt = None

    # Move from Start state to SELECT_CARD
    self.fsm.pump(None)

  
  def select_card_action(self, card):
    """ Appends card to action_unit list and marks it used.
    """
    try:
      i = self.hand_cards.index((card, True))
    except:
      raise InvalidChoiceException('Jack not found')

    self.hand_cards[i] = (card, False)
    self.action_units.append([card])


  def start_transition(self, choice):
    return 'SELECT_CARD'


  def finished_arrival(self):
    self.done = True
    self.action = message.GameAction(
        message.LEADROLE, self.role, len(self.action_units),
        *itertools.chain(*self.action_units))
    

  def jack_role_arrival(self):
    self.choices = izip_longest(card_manager.get_all_roles(), [], fillvalue=True)
    self.prompt = 'Select role for Jack'


  def jack_role_transition(self, choice):
    try:
      self.role = card_manager.get_all_roles()[choice]
    except IndexError:
      raise InvalidChoiceException('Invalid choice.')

    self.select_card_action('Jack')
    
    new_state = 'PALACE_CARDS' if self.has_palace else 'FINISHED'

    return new_state

 
  def finish_petition(self):
    self.action_units.append(self.petition_cards)

    for card in self.petition_cards:
      i = self.hand_cards.index((card, True))
      self.hand_cards[i] = (card, False)

    self.petition_cards = []


  def petition_role_arrival(self):
    self.choices = izip_longest(card_manager.get_all_roles(), [], fillvalue=True)
    self.prompt = 'Select role for Petition'


  def petition_role_transition(self, choice):
    try:
      self.role = card_manager.get_all_roles()[choice]
    except IndexError:
      raise InvalidChoiceException('Invalid choice.')

    self.finish_petition()

    new_state = 'PALACE_CARDS' if self.has_palace else 'FINISHED'

    return new_state

 
  def first_petition_arrival(self):
    # TODO: update this so it's only groups of 3(2) that can be selected
    self.choices = [ (det(c[0]), c[1]) for c in self.hand_cards if c[0] != 'Jack' ]
    self.choices.append(('Cancel petition', True))
    
    self.prompt = 'Select cards for petition'


  def first_petition_transition(self, choice):
    selectable_cards = [c[0] for c in self.hand_cards if c[1] and c[0] != 'Jack']

    if choice == len(selectable_cards):
      # Cancel petition. Determine where we came from with self.role
      new_state = 'PALACE_CARDS' if self.action_units else 'SELECT_CARD'
      
    elif choice >= 0 and choice < len(selectable_cards):
      card = selectable_cards[choice]
      self.petition_cards.append(card)

      new_state = 'MORE_PETITIONS'

    else:
      raise InvalidChoiceException('Invalid choice')

    return new_state


  def more_petitions_arrival(self):
    choices = self.get_petition_filtered_hand()

    self.choices = [(det(c[0]),c[1]) for c in choices]
    self.choices.append(('Cancel petition', True))


  def more_petitions_transition(self, choice):
    choices = self.get_petition_filtered_hand()

    selectable_cards = [c[0] for c in choices if c[1]]

    if choice == len(selectable_cards):
      self.petition_cards = []

      # Cancel petition. Determine origin state
      # by checking if the role's been defined.
      new_state = 'PALACE_CARDS' if self.action_units else 'SELECT_CARD'
      
    elif choice >= 0 and choice < len(selectable_cards):
      card = selectable_cards[choice]
      self.petition_cards.append(card)

      if len(self.petition_cards) == self.petition_count:
        if self.role:
          self.finish_petition()
          new_state = 'PALACE_CARDS' if self.has_palace else 'FINISHED'

        else:
          new_state = 'PETITION_ROLE'

      else:
        new_state = 'MORE_PETITIONS'

    else:
      raise InvalidChoiceException('Invalid choice')

    return new_state


  def select_card_arrival(self):
    self.choices = [ (det(c[0]), c[1]) for c in self.hand_cards]
    self.choices.append(('Petition', True))

    self.prompt = 'Select card to lead'


  def select_card_transition(self, choice):
    selectable_cards = [c[0] for c in self.hand_cards if c[1]]

    if choice == len(selectable_cards):
      new_state = 'FIRST_PETITION'

    elif choice>=0 and choice<len(selectable_cards):
      card = selectable_cards[choice]

      if not self.role and card == 'Jack':
        new_state = 'JACK_ROLE'

      else:
        if not self.role and card != 'Jack':
          self.role = card_manager.get_role_of_card(card)
      
        self.select_card_action(card)

        new_state = 'PALACE_CARDS' if self.has_palace else 'FINISHED'

    else:
      raise InvalidChoiceException('Invalid choice')

    return new_state


  def palace_cards_arrival(self):
    self.choices = [ (det(c[0]), c[1]) for c in self.hand_cards ]
    self.choices.append(('Petition', True))
    self.choices.append(('Skip further Palace actions', True))

    self.prompt = 'Select additional palace actions ({0:d} actions)'.format(
        len(self.action_units))


  def palace_cards_transition(self, choice):
    selectable_cards = [c[0] for c in self.hand_cards if c[1]]

    if choice == len(selectable_cards):
      new_state = 'FIRST_PETITION'

    elif choice == len(selectable_cards) + 1:
      new_state = 'FINISHED'

    elif choice >= 0 and choice<len(selectable_cards):
      card = selectable_cards[choice]

      i = self.hand_cards.index((card, True))
      self.hand_cards[i] = (card, False)

      self.action_units.append([card])
      new_state = 'PALACE_CARDS'

    else:
      raise InvalidChoiceException('Invalid choice')

    return new_state


  def get_petition_filtered_hand(self):
    # TODO: update this so it's only groups of 3(2) that can be selected
    petition_cpy = copy.deepcopy(self.petition_cards)
    if self.petition_cards:
      petition_role = card_manager.get_role_of_card(self.petition_cards[0])
    else:
      petition_role = None

    choices = []

    for card, selectable in self.hand_cards:
      role_mismatch = petition_role and card != 'Jack' and \
          petition_role != card_manager.get_role_of_card(card)

      if card == 'Jack' or role_mismatch or not selectable:
        choices.append((card,False))

      elif card in petition_cpy and selectable:
        choices.append((card,False))
        petition_cpy.remove(card)

      else:
        choices.append((card, selectable))

    return choices
  

  def get_choices(self):
    return self.choices


  def make_choice(self, choice):
    self.fsm.pump(choice)


class FollowRoleActionBuilder(object):
  def __init__(self, role, hand, has_palace, petition_count):

    self.fsm = StateMachine()

    self.fsm.add_state('START', None, self.start_transition)
    self.fsm.add_state('SELECT_CARD',
        self.select_card_arrival, self.select_card_transition)
    self.fsm.add_state('PALACE_CARDS',
        self.palace_cards_arrival, self.palace_cards_transition)
    self.fsm.add_state('FIRST_PETITION',
        self.first_petition_arrival, self.first_petition_transition)
    self.fsm.add_state('MORE_PETITIONS',
        self.more_petitions_arrival, self.more_petitions_transition)
    self.fsm.add_state('FINISHED', self.finished_arrival, None, True)

    self.fsm.set_start('START')

    self.hand_cards = [(c, True) for c in sorted(hand, card_manager.cmp_jacks_first)]

    self.role = role
    self.has_palace = has_palace
    self.petition_count = petition_count

    self.choices = None
    
    self.action_units = [] # list of lists: [card1, card2, ... ]
    self.petition_cards = []

    self.done = False
    self.prompt = None

    # Move from Start state to SELECT_CARD
    self.fsm.pump(None)


  def select_card_action(self, card):
    """ Appends card to action_unit list and marks it used.
    """
    try:
      i = self.hand_cards.index((card, True))
    except:
      raise InvalidChoiceException(card + ' not found')

    self.hand_cards[i] = (card, False)
    self.action_units.append([card])


  def start_transition(self, choice):
    return 'SELECT_CARD'


  def finished_arrival(self):
    self.done = True
    if len(self.action_units) == 0:
      self.action = message.GameAction(message.FOLLOWROLE, True, 0, None)
    else:
      self.action = message.GameAction(
          message.FOLLOWROLE, False, len(self.action_units),
          *itertools.chain(*self.action_units))
 

  def finish_petition(self):
    self.action_units.append(self.petition_cards)

    for card in self.petition_cards:
      i = self.hand_cards.index((card, True))
      self.hand_cards[i] = (card, False)

    self.petition_cards = []


  def first_petition_arrival(self):
    # TODO: update this so it's only groups of 3(2) that can be selected
    self.choices = [ (det(c[0]), c[1]) for c in self.hand_cards if c[0] != 'Jack' ]
    self.choices.append(('Cancel petition', True))
    
    self.prompt = 'Select cards for petition'


  def first_petition_transition(self, choice):
    selectable_cards = [c[0] for c in self.hand_cards if c[1] and c[0] != 'Jack']

    if choice == len(selectable_cards):
      # Cancel petition. Determine where we came from with self.role
      new_state = 'PALACE_CARDS' if self.action_units else 'SELECT_CARD'
      
    elif choice >= 0 and choice < len(selectable_cards):
      card = selectable_cards[choice]
      self.petition_cards.append(card)

      new_state = 'MORE_PETITIONS'

    else:
      raise InvalidChoiceException('Invalid choice')

    return new_state


  def more_petitions_arrival(self):
    choices = self.get_petition_filtered_hand()

    self.choices = [(det(c[0]),c[1]) for c in choices]
    self.choices.append(('Cancel petition', True))


  def more_petitions_transition(self, choice):
    choices = self.get_petition_filtered_hand()

    selectable_cards = [c[0] for c in choices if c[1]]

    if choice == len(selectable_cards):
      self.petition_cards = []

      # Cancel petition. Determine origin state
      # by checking if an action has been added
      new_state = 'PALACE_CARDS' if self.action_units else 'SELECT_CARD'
      
    elif choice >= 0 and choice < len(selectable_cards):
      card = selectable_cards[choice]
      self.petition_cards.append(card)

      if len(self.petition_cards) == self.petition_count:
        self.finish_petition()
        new_state = 'PALACE_CARDS' if self.has_palace else 'FINISHED'

      else:
        new_state = 'MORE_PETITIONS'

    else:
      raise InvalidChoiceException('Invalid choice')

    return new_state


  def select_card_arrival(self):
    self.choices = []
    for card, selectable in self.hand_cards:
      if selectable and (card == 'Jack' or self.role == card_manager.get_role_of_card(card)):
        self.choices.append((det(card), selectable))
      else:
        self.choices.append((det(card), False))

    self.choices.append(('Petition', True))
    self.choices.append(('Thinker instead', True))

    self.prompt = 'Select card to follow with'


  def select_card_transition(self, choice):
    selectable_cards = []
    for card, selectable in self.hand_cards:
      if selectable and (card == 'Jack' or self.role == card_manager.get_role_of_card(card)):
        selectable_cards.append(card)

    if choice == len(selectable_cards):
      new_state = 'FIRST_PETITION'

    elif choice == len(selectable_cards)+1:
      new_state = 'FINISHED'

    elif choice>=0 and choice<len(selectable_cards):
      card = selectable_cards[choice]

      self.select_card_action(card)

      new_state = 'PALACE_CARDS' if self.has_palace else 'FINISHED'

    else:
      raise InvalidChoiceException('Invalid choice')

    return new_state


  def palace_cards_arrival(self):
    self.choices = []
    for card, selectable in self.hand_cards:
      if selectable and (card == 'Jack' or self.role == card_manager.get_role_of_card(card)):
        self.choices.append((det(card), selectable))
      else:
        self.choices.append((det(card), False))

    self.choices.append(('Petition', True))
    self.choices.append(('Skip further Palace actions', True))

    self.prompt = 'Select additional palace actions ({0:d} actions)'.format(
        len(self.action_units))


  def palace_cards_transition(self, choice):
    selectable_cards = []
    for card, selectable in self.hand_cards:
      if selectable and (card == 'Jack' or self.role == card_manager.get_role_of_card(card)):
        selectable_cards.append(card)

    if choice == len(selectable_cards):
      new_state = 'FIRST_PETITION'

    elif choice == len(selectable_cards) + 1:
      new_state = 'FINISHED'

    elif choice >= 0 and choice<len(selectable_cards):
      card = selectable_cards[choice]

      i = self.hand_cards.index((card, True))
      self.hand_cards[i] = (card, False)

      self.action_units.append([card])
      new_state = 'PALACE_CARDS'

    else:
      raise InvalidChoiceException('Invalid choice')

    return new_state


  def get_petition_filtered_hand(self):
    # TODO: update this so it's only groups of 3(2) that can be selected
    petition_cpy = copy.deepcopy(self.petition_cards)
    if self.petition_cards:
      petition_role = card_manager.get_role_of_card(self.petition_cards[0])
    else:
      petition_role = None

    choices = []

    for card, selectable in self.hand_cards:
      role_mismatch = petition_role and card != 'Jack' and \
          petition_role != card_manager.get_role_of_card(card)

      if card == 'Jack' or role_mismatch or not selectable:
        choices.append((card,False))

      elif card in petition_cpy and selectable:
        choices.append((card,False))
        petition_cpy.remove(card)

      else:
        choices.append((card, selectable))

    return choices
  

  def get_choices(self):
    return self.choices


  def make_choice(self, choice):
    self.fsm.pump(choice)



class SingleChoiceActionBuilder(object):
  """ Gets a simple response from a list.
  """
  def __init__(self, action_type, choices):
    """ The parameter choices is a list of tuples with the value of
    the choice for the GameAction and a string presented in the choice list.
    
    For example:

    [('Rubble', 'Build on a Rubble site'),
     ('Concrete, 'cannot build on a Concrete site')]
    """
    self.choices = choices
    self.action_type = action_type
    self.action = None
    self.done = False
    self.prompt = None

  def get_choices(self):
    """ Returns (choices, prompt), where choices is a list
    of tuples of the choice and whether it's selectable.
    
    Eg. choices = [('Rubble', False), ('Concrete', True)]
    """
    return [(e[1],True) for e in self.choices]

  def make_choice(self, choice):
    if(choice < 0 or choice >= len(self.choices)):
      raise InvalidChoiceException('Index out of range: {0:d}. Enter [0-{1:d}]'
                               .format(choice, len(self.choices)-1))

    item = self.choices[choice][0]

    self.action = message.GameAction(self.action_type, item)
    self.done = True


class Client(object):
  """ Rather than interact with the player via the command line and stdout,
  this client is broken out with an interface for each of the game decisions
  a player must make.
  
  The client needs a model of the game state to be able
  to create appropriate dialogs/gui. For a simplistic start, this game model
  is a copy of the official Game object, which is really just a copy of the
  GameState object. The final version will update known game information with
  notifications of other players' moves from the server.

  The _Dialog() methods mostly shouldn't have arguments,
  maybe with the exception for building actions (out-of-town).
  """
  
  def __init__(self):
    self.game = gtr.Game()
    self.game.game_state = None
    self.player_id = None
    self.builder = None

  def get_player(self):
    return self.game.game_state.players[self.player_id]

  def update_game_state(self, gs):
    """ Updates the game state
    """
    self.game.game_state = gs
    self.game.game_state.show_public_game_state()

    if self.player_id == self.game.game_state.get_active_player_index():

        method_name = message.get_action_name(self.game.game_state.expected_action)
        method = getattr(self, 'action_' + method_name)

        method()

        self.show_choices(self.builder.get_choices(), self.builder.prompt)

    else:
        print 'Waiting on player {0!s}'.format(self.game.game_state.get_active_player_index())


  def make_choice(self, choice):
    """ Makes a selection of a menu item. If the action is completed,
    then return the action and reset the builder.
    If the action is not complete, return None.
    """
    # The GUI uses lists indexed from 1, but we need 0-indexed
    try:
      self.builder.make_choice(choice-1)
    except InvalidChoiceException:
      sys.stderr.write('Invalid choice. Enter [1-{0:d}]\n'.format(
        len(self.builder.choices)))

    if self.builder.done:
      action = self.builder.action
      lg.info('Finished action ' + repr(action))
      self.builder = None
      return action
    else:
      self.show_choices(self.builder.get_choices())
      return None


  def show_choices(self, choices_list, prompt=None):
    """ Returns the index in the choices_list selected by the user or
    raises a StartOverException or a CancelDialogExeption.
    
    The choices_list is either a list of choices, or a list of
    tuples of (choice, selectable) - eg. ('Rubble', False) - such
    that a choice can be listed but unselectable.
    """
    i_choice = 1
    for e in choices_list:
      try:
        choice = e[0]
        selectable = e[1]
      except TypeError:
        choice = e
        selectable = True

      if selectable:
        lg.info('  [{0:2d}] {1}'.format(i_choice, choice))
        i_choice+=1
      else:
        lg.info('       {0}'.format(choice))

    if prompt is not None:
      print prompt
    else:
      print 'Please make a selection:'
    


  def action_thinkerorlead(self):
    """ Asks whether the player wants to think or lead at the start of their
    turn.
    """
    self.builder = SingleChoiceActionBuilder(message.THINKERORLEAD,
                     [(True, 'Thinker'), (False, 'Lead a role')])


  def action_usesenate(self):
    """ Asks whether the player wants to think or lead at the start of their
    turn.
    """
    i = self.game.game_state.kip_index
    p_name = self.game.game_state.players[p].name

    self.builder = SingleChoiceActionBuilder(message.USESENATE,
        [(True, 'Take {0:d}\'s Jack with Senate'.format(p_name)),
         (False, 'Don\'t take Jack')])


  def action_thinkertype(self):
    """ Asks for the type of thinker.
    """
    p = self.get_player()

    n_cards = max(self.game.get_max_hand_size(p) - len(p.hand), 1)
    cards_str = '{0:d} card{1}'.format(n_cards, 's' if n_cards==1 else '')

    self.builder = SingleChoiceActionBuilder(message.THINKERTYPE,
        [(True, 'Thinker for Jack'),
         (False, 'Thinker for '+cards_str)])


  def action_uselatrine(self):
    """ Asks which card, if any, the player wishes to use with the 
    Latrine before thinking.
    """
    #lg.info('Choose a card to discard with the Latrine.')

    sorted_hand = sorted(self.get_player().hand)
    card_choices = [(c, gtrutils.get_detailed_card_summary(c)) for c in sorted_hand]
    card_choices.insert(0, (None, 'Skip discard'))

    self.builder = SingleChoiceActionBuilder(message.USELATRINE, card_choices)

    self.builder.prompt = 'Select a card to discard with the Latrine'


  def action_skipthinker(self):
    """ Asks if the player wants to skip the thinker action.
    """
    choices = [(True, 'Perform thinker'), (False, 'Skip thinker')]

    self.builder = SingleChoiceActionBuilder(message.SKIPTHINKER, choices)

    self.builder.prompt = 'Skip thinker action?'


  def action_baroraqueduct(self):
    self.builder = SingleChoiceActionBuilder(message.BARORAQUEDUCT,
        [(True, 'Bar then Aqueduct'), (False, 'Aqueduct then Bar')])

    self.builder.prompt = 'Use the Bar or the Aqueduct first?'
    #TODO: We still have to answer this even if we want to skip both.


  def action_usevomitorium(self):
    """ Asks if the player wants to discard their hand with the Vomitorium.

    Returns True if player uses the Vomitorium, False otherwise.
    """
    self.builder = SingleChoiceActionBuilder(message.USEVOMITORIUM,
        [(True, 'Discard all'), (False, 'Skip Vomitorium')])
    self.builder.prompt = 'Discard hand with Vomitorium?'


  def action_patronfrompool(self):
    p = self.get_player()
    self.builder = SingleChoiceActionBuilder(message.PATRONFROMPOOL,
        [ (c, det(c)) for c in sorted(self.game.game_state.pool) ] + \
        [ (None, 'Skip Patron from pool') ])

    self.builder.prompt = \
        'Performing Patron, choose a client from pool (Clientele {}/{})'.format(
            str(p.get_n_clients()), str(self.game.get_clientele_limit(p)))


  def action_patronfromdeck(self):
    p = self.get_player()
    self.builder = SingleChoiceActionBuilder(message.PATRONFROMHAND,
        [ (True, 'Patron from the deck'), 
          (False, 'Skip Patron from deck') ])

    self.builder.prompt = \
        'Performing Patron, take a card from the deck? (Clientele {}/{})'.format(
            str(p.get_n_clients()), str(self.game.get_clientele_limit(p)))


  def action_patronfromhand(self):
    p = self.get_player()
    cards = sorted([c for c in hand if c != 'Jack'])
    self.builder = SingleChoiceActionBuilder(message.PATRONFROMHAND,
        [ (c, det(c)) for c in cards ] + 
        [ (None, 'Skip Patron from hand') ])

    self.builder.prompt = \
        'Performing Patron, choose a client from pool (Clientele {}/{})'.format(
            str(p.get_n_clients()), str(self.game.get_clientele_limit(p)))


  def action_usefountain(self):
    self.builder = SingleChoiceActionBuilder(message.USEFOUNTAIN,
        [(True, 'Use Fountain'), (False, 'Don\'t use Fountain')])
    self.builder.prompt = 'Use Fountain to Craftsman from deck?'


  def action_fountain(self):
    """ The Fountain allows you to draw a card from the deck, then
    choose whether to use the card with a craftsman action. The player
    is allowed to just keep (draw) the card.
    
    This function returns a tuple (skip, building, material, site),
    with the following elements:
      1) Whether the player skips the action or not (drawing the card)
      2) The building to be started or added to
      3) The material to be added to an incomplete building
      4) The site to start a building on.

    The material will always be the Fountain card or None, and the building might be
    the Fountain card.
    """
    p = self.get_player()

    skip, building, material, site = (False, None, None, None)

    material_of_card = card_manager.get_material_of_card(p.fountain_card)
    
    card_choices = \
      [str(b) for b in p.get_incomplete_buildings()
      if self.game.check_building_add_legal(p, str(b), p.fountain_card)]

    if not p.owns_building(p.fountain_card):
      card_choices.insert(0, 'Start {} buidling'.format(p.fountain_card))

    if len(card_choices) == 0:
      lg.warn('Can\'t use {} with a craftsman action'.format(p.fountain_card))
      return message.GameAction(message.FOUNTAIN, True, None, None, None)

    lg.info('Performing Craftsman with {}, choose a building option:'
                 .format(p.fountain_card))

    choices = ['Use {} to start or add to a building'.format(p.fountain_card),
               'Don\'t play card, draw and skip action instead.']
    choice_index = self.choices_dialog(choices)

    if choice_index == 1:
      lg.info('Skipping Craftsman action and drawing card')
      return message.GameAction(message.FOUNTAIN, True, None, None, None)

    card_index = self.choices_dialog(card_choices, 'Select a building option')
    if card_index == 0: # Starting a new building
      building = p.fountain_card

      if building == 'Statue':
        sites = card_manager.get_all_materials()
        site_index = self.choices_dialog(sites)
        site = sites[site_index]
      else:
        site = card_manager.get_material_of_card(building)

    else: # Adding to a building from hand
      building = card_choices[card_index-1]
      material = p.fountain_card

    return message.GameAction(message.FOUNTAIN, False, building, material, site)

  def action_legionary(self):
    p = self.get_player()
    lg.info('Card to use for legionary:')
    hand = p.hand
    sorted_hand = sorted(hand)
    card_choices = [gtrutils.get_detailed_card_summary(card) for card in sorted_hand]

    card_index = self.choices_dialog(card_choices, 
        'Select a card to use to demand material')
    card_from_hand = sorted_hand[card_index]

    lg.info('Using card %s' % gtrutils.get_detailed_card_summary(card_from_hand))
    return message.GameAction(message.LEGIONARY, card_from_hand)
        

  def action_givecards(self):
    return message.GameAction(message.GIVECARD, None)


  def action_usesewer(self):
    p = self.get_player()
    done=False
    cards_to_move=[]
    choices=['All', 'None']
    choices.extend([gtrutils.get_detailed_card_summary(card) 
                    for card in p.camp if card is not 'Jack'])
    while not done:
      lg.info('Do you wish to use your Sewer?')

      card_index = self.choices_dialog(choices, 'Select a card to take into your stockpile')
      if card_index == 0:
        cards_to_move.extend(choices[2:])
      elif card_index > 1:
        cards_to_move.append(choices.pop(card_index))
      else:
        done=True
    
    return message.GameAction(message.USESEWER, cards_to_move)


  def action_laborer(self):
    """ Returns (card_from_pool, card_from_hand).
    """
    player = self.get_player()

    has_dock = self.game.player_has_active_building(player, 'Dock')
    pool = self.game.game_state.pool
    hand = player.hand

    self.builder = LaborerActionBuilder(pool, hand, has_dock)


  def action_stairway(self):
    """
    Asks the player if they wish to use the Stairway and returns
    
    (player, building, material, from_pool)

    player: the player that owns the building
    building: the name (string) of the building
    material: name of the material card to use
    from_pool: bool to use the Archway to take from the pool
    """
    p = self.get_player()
    possible_buildings = [(pl, b) for pl in self.game.game_state.players
                          for b in pl.get_completed_buildings()
                          if pl is not p
                          ]
    possible_buildings = sorted(possible_buildings, None, lambda x: x[0].name.lower() + str(x[1]).lower())
    lg.info('Use Stairway?')
    building_names = [pl.name + '\'s ' + str(b) for (pl,b) in possible_buildings]
    choices = sorted(building_names)
    choices.insert(0, 'Don\'t use Stairway')
    choice_index = self.choices_dialog(choices, 'Select option for Stairway')
    
    player_name, building_name, material, from_pool = None, None, None, False
    if choice_index != 0:
      player, building = possible_buildings[choice_index-1]
      player_name = player.name
      building_name = building.foundation
      
      has_archway = self.game.player_has_active_building(p, 'Archway')
      
      sorted_stockpile = sorted(p.stockpile)
      lg.info('Choose a material to add from your stockpile:')
      card_choices = [gtrutils.get_detailed_card_summary(card) for card in sorted_stockpile]

      if has_archway:
        sorted_pool = sorted(self.game.game_state.pool)
        pool_choices = ['[POOL]' + gtrutils.get_detailed_card_summary(card) for card in sorted_pool]
        card_choices.extend(pool_choices)

      card_index = self.choices_dialog(card_choices, 'Select a material to add')

      if card_index >= len(sorted_stockpile):
        from_pool = True
        material = sorted_pool[card_index - len(sorted_stockpile)]
      else:
        material = sorted_stockpile[card_index]

    return message.GameAction(message.STAIRWAY, player_name, building_name, material, from_pool)

  def action_architect(self):
    """ Returns (building, material, site, from_pool) to be built.

    If the action is to be skipped, returns None, None, None
    """
    building, material, site, from_pool = None, None, None, False
    p = self.get_player()

    lg.info('Performing Architect, choose a building option:')
    card_choices = sorted(p.get_incomplete_building_names())
    card_choices.insert(0, 'Start a new buidling')
    card_choices.insert(0, 'Skip action')

    card_index = self.choices_dialog(card_choices, 'Select a building option')
    if card_index == 1: # Starting a new building
      sorted_hand = sorted(p.hand)
      lg.info('Choose a building to start from your hand:')
      card_choices = [gtrutils.get_detailed_card_summary(card) for card in sorted_hand]

      card_index = self.choices_dialog(card_choices, 'Select a building to start')
      building = sorted_hand[card_index]

      if building == 'Statue':
        sites = card_manager.get_all_materials()
        site_index = self.choices_dialog(sites)
        site = sites[site_index]
      else:
        site = card_manager.get_material_of_card(building)

    elif card_index > 1: # Adding to a building from stockpile
      building = card_choices[card_index]
      
      has_archway = self.game.player_has_active_building(p, 'Archway')
      
      sorted_stockpile = sorted(p.stockpile)
      lg.info('Choose a material to add from your stockpile:')
      card_choices = [gtrutils.get_detailed_card_summary(card) for card in sorted_stockpile]

      if has_archway:
        sorted_pool = sorted(self.game.game_state.pool)
        pool_choices = ['[POOL]' + gtrutils.get_detailed_card_summary(card) for card in sorted_pool]
        card_choices.extend(pool_choices)

      card_index = self.choices_dialog(card_choices, 'Select a material to add')

      if card_index >= len(sorted_stockpile):
        from_pool = True
        material = sorted_pool[card_index - len(sorted_stockpile)]
      else:
        material = sorted_stockpile[card_index]

    return message.GameAction(message.ARCHITECT, building, material, site, from_pool)

  def action_craftsman(self):
    """ Returns (building, material, site) to be built.
    """
    p = self.get_player()
    building, material, site = None, None, None

    lg.info('Performing Craftsman, choose a building option:')
    card_choices = p.get_incomplete_building_names()
    card_choices.insert(0, 'Start a new buidling')

    card_index = self.choices_dialog(card_choices, 'Select a building option')
    if card_index == 0: # Starting a new building
      sorted_hand = sorted(p.hand)
      lg.info('Choose a building to start from your hand:')
      card_choices = [gtrutils.get_detailed_card_summary(card) for card in sorted_hand]

      card_index = self.choices_dialog(card_choices, 'Select a building to start')
      building = sorted_hand[card_index]

      if building == 'Statue':
        sites = card_manager.get_all_materials()
        site_index = self.choices_dialog(sites)
        site = sites[site_index]
      else:
        site = card_manager.get_material_of_card(building)

    else: # Adding to a building from hand
      building = card_choices[card_index]
      
      sorted_hand = sorted(p.hand)
      lg.info('Choose a material to add from your hand:')
      card_choices = [gtrutils.get_detailed_card_summary(card) for card in sorted_hand]

      card_index = self.choices_dialog(card_choices, 'Select a material to add')
      material = sorted_hand[card_index]

    return message.GameAction(message.CRAFTSMAN, building, material, site)

  def action_merchant(self):
    """ Prompts for which card to get from the pool and hand for a Merchant
    action.

    There are flags for the player having an Basilica or Atrium. These
    cause the dialog to prompt for card from the hand or replace the 
    normal merchant action with a draw from the deck.

    There is a parameter that specifies the card limit for the vault. This
    is the number of slots left in the vault.
    """
    p = self.get_player()

    vault_limit = p.get_influence_points()

    card_limit = vault_limit - len(p.vault)
    if self.game.player_has_active_building(p, 'Market'): card_limit += 2

    has_atrium = self.game.player_has_active_building(p, 'Atrium')
    has_basilica = self.game.player_has_active_building(p, 'Basilica')

    card_from_stockpile, card_from_hand, card_from_deck  = (None,None,False)

    sorted_stockpile = sorted(player.stockpile)
    if len(sorted_stockpile) > 0 or has_atrium:
      lg.info('Performing Merchant, choose a card from stockpile (Vault {}/{})'.format(
        str(len(p.vault)), str(card_limit)))
      card_choices = [gtrutils.get_detailed_card_summary(card) for card in sorted_stockpile]
      card_choices.insert(0,'Skip this action')
      card_choices.insert(1,'Take card from top of deck')

      card_index = self.choices_dialog(card_choices, 'Select a card to take into your vault')
      if card_index == 1:
        card_from_deck = True
        card_limit -= 1 
      elif card_index > 1:
        card_from_stockpile = sorted_stockpile[card_index-2]
        card_limit -= 1

    sorted_hand = sorted([card for card in p.hand if card != 'Jack'])
    if card_limit>0 and has_basilica and len(sorted_hand)>0:
      lg.info('Choose a card from your hand:')
      card_choices = [gtrutils.get_detailed_card_summary(card) for card in sorted_hand]
      card_choices.insert(0,'Skip this action')

      card_index = self.choices_dialog(card_choices, 'Select a card to take into your vault')
      if card_index > 0:
        card_from_hand = sorted_hand[card_index-1]

    return message.GameAction(message.MERCHANT, card_from_stockpile, card_from_hand, card_from_deck)



  def action_leadrole(self):
    hand = self.get_player().hand
    has_palace = self.game.player_has_active_building(self.get_player(), 'Palace')
    petition_count = 2 if self.game.player_has_active_building(self.get_player(), 'Circus') else 3
    self.builder = LeadRoleActionBuilder(hand, has_palace, petition_count)




  def action_followrole(self):
    hand = self.get_player().hand
    has_palace = self.game.player_has_active_building(self.get_player(), 'Palace')
    petition_count = 2 if self.game.player_has_active_building(self.get_player(), 'Circus') else 3
    role = self.game.game_state.role_led
    self.builder = FollowRoleActionBuilder(role, hand, has_palace, petition_count)

# vim: ts=8:sts=2:sw=2:et