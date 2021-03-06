"""Redis database interface for cloaca.

All database manipulation is done with this API.

Integer types
-------------
Redis stores all keys and values as strings, so for integral types,
like user and game IDs, this API converts to Python int. This applies
to the following:

    user_id
    game_id

which are returned by the functions:

    retrieve_user()
    retrieve_user_id_from_username()
    retrieve_userid_from_session_auth()

    retrieve_games_hosted_by_user



Users
=====
Users are stored as hashes, with the following fields:

    <username> : user name
    <date_added> : Unix time stamp, UTC
    <last_login> : Unix time stamp, UTC
    <auth> : authentication token (salted password hash)
    <session_auth> : session token

User hashes and specific fields are retrieved with the following:

    retrieve_user() : get the entire user hash
    retrieve_user_session_auth()
    retrieve_user_auth()

Users are created with:

    add_user()

which calls `register_user()` to evaluate the Lua script that registers a user.
The method `register_user()` should not be called directly.

User info is modified with:

    update_user_last_login()
    update_user_session()

Usernames
=========
A reverse-lookup table of username to user_id is accessed with:

    retrieve_user_id_from_username()


Sessions
========
Session tokens are stored in the user hash and in a reverse-mapping
of session token to user_id.
User session tokens are obtained with the functions described
in the "Users" section. To read session token from the user id:

    retrieve_user_session_auth()

and to update the session token:

    update_user_session()

The reverse mapping is accessed with:

    retrieve_userid_from_session_auth()

Games
=====
Games are hashes with the following fields:

    <game_id>
    <host> : host user ID
    <date_created> : unix time stamp, UTC
    <game_data> : encoded game state as a string. (Not necessarily JSON!)

Games are created via the function:

    create_game_with_host()

which calls the `create_game` Lua script. This script also pushes the game ID
onto the list of games and the list of games hosted by that particular user.
Also, the user ID is pushed onto the list of game_hosts.

Games are retrieved and stored with the functions:

    retrieve_game()
    retrieve_games()
    retrieve_games_hosted_by_user() : Returns list of game IDs
    retrieve_latest_games() : Returns list of game IDs
    store_game()


Game logs
=========
Each game has a list of log messages generated by the game engine describing
what has happened in the game. These are stored separately from the game itself.
They are accessed and modified using the following functions:

    retrieve_log_length()
    retrieve_log_messages()
    append_log_messages()

Logs are stored as Redis Lists, with the key "game_log:<game_id>".
The most common use cases are loading the last ~50 messages when a game
is loaded, and appending the new messages as a game is modified.
Occasionally, when the user reconnects, and scrolls up to see
old messages, the entire list may need to be traversed, which has
time complexity O(N).

Game actions
============
Actions are recorded with the function:

    set_game_action()

Actions are encoded in the encode_action module to a JSON string after converting
roles and materials (sites) to integers.
In Redis, the encoded actions are pushed to a hash of actions for each game, with
the index of the action as the field name.
"""
import time

from tornado import gen
import tornadis
from tornadis.exceptions import TornadisException

from cloaca.error import GTRDBError

from cloaca import lua_scripts

GAMEID = 'gameid'
GAMEPREFIX = 'game:'
GAMES = 'games'
GAMES_HOSTED_PREFIX = 'games_hosted:'
GAMES_JOINED_PREFIX = 'games_joined:'
GAME_HOSTS = 'game_hosts'
GAME_DATA_KEY = 'game_data'

GAME_MOVE_PREFIX = 'game_actions:'

LOG_PREFIX = 'game_log:'

USERID = 'userid'
USERPREFIX='user:'
USERNAMES='usernames'

SESSIONS='sessions'
SESSION_AUTH_LENGTH_BYTES=16

db = None

def connect(host, port, prefix):
    global db
    if db is None:
        db = GTRDBTornadis(host, port, prefix)
    return db


class GTRDBTornadis(object):

    def __init__(self, host='localhost', port=6379, prefix=''):
        self.r = tornadis.Client(host=host, port=port, autoconnect=True)
        self.prefix = prefix

        self.scripts_sha = {}


    @gen.coroutine
    def load_scripts(self):
        sha = yield self.r.call("SCRIPT", "LOAD", lua_scripts.REGISTER_USER)
        self.scripts_sha['register'] = sha

        sha = yield self.r.call("SCRIPT", "LOAD", lua_scripts.CREATE_GAME)
        self.scripts_sha['create_game'] = sha


    @gen.coroutine
    def select(self, selected_db):
        yield self.r.call('SELECT', selected_db)


    @gen.coroutine
    def append_log_messages(self, game_id, messages):
        """Append IN ORDER the iterable of <messages> to the log for game
        specifed by <game_id>. Return the updated length of the list.

        Messages are arbitrary strings.

        Messages should be in chronological order. That is, the first element
        of <messages> will be the oldest log message.
        """
        # The messages are passed through function arguments, so
        # we should limit how many are passed at once, since Python
        # limits the number of function args. (But what about *args?)
        # Break messages up into 30-message chunks.
        def chunks(l, n):
            """Yield successive n-sized chunks from l."""
            for i in xrange(0, len(l), n):
                yield l[i:i + n]

        log_length = None
        if len(messages) == 0:
            log_length = yield self.r.call('LLEN',
                    self.prefix+LOG_PREFIX+str(game_id))
        else:
            for chunk in chunks(messages, 30):
                log_length = yield self.r.call('LPUSH',
                        self.prefix+LOG_PREFIX+str(game_id), *chunk)

        # List lengths are longs as returned by Tornadis/Redis. Convert to int.
        raise gen.Return(int(log_length))


    @gen.coroutine
    def retrieve_log_length(self, game_id):
        log_length = yield self.r.call('LLEN',
                self.prefix+LOG_PREFIX+str(game_id))
        # List lengths are longs as returned by Tornadis/Redis. Convert to int.
        raise gen.Return(int(log_length))


    @gen.coroutine
    def retrieve_log_messages(self, game_id, n_messages, n_start):
        """Return <n_messages> log messages from game with ID <game_id>
        starting at the <n_start>'th message (default 0).

        If n_messages larger than the list, all messages are returned.
        If n_start is >= than the length, an empty list is returned.
        """
        # LRANGE includes the last element requested, so
        #   LRANGE list 0 10
        # returns 11 elements.
        # Since we're adding by LPUSH, if we want to start at S
        # and get N messages:
        #
        # S=2, N=3
        # Message # :
        # 5  4  3  2  1  0
        #    <----->
        #-6 -5 -4 -3 -2 -1
        #
        # we need to issue command LRANGE <list> -N-S -S-1
        #
        # These messages will still be in the wrong order.
        # 
        messages = yield self.r.call('LRANGE',
                self.prefix+LOG_PREFIX+str(game_id),
                -(n_start+n_messages), -n_start-1)
        raise gen.Return(reversed(messages))


    @gen.coroutine
    def create_game_with_host(self, host_user_id):
        """Create a new game hosted by user with ID host_user_id.
        Return the new game ID.
        
        First verifies that the host user exists.
        """
        game_id = yield self.r.call('EVALSHA', self.scripts_sha['create_game'],
                5,
                self.prefix+GAMEID,
                self.prefix+USERPREFIX+str(host_user_id),
                self.prefix+GAMES_HOSTED_PREFIX+str(host_user_id),
                self.prefix+GAMES,
                self.prefix+GAME_HOSTS,
                host_user_id)


        if not game_id:
            raise GTRDBError('Host user (ID {0:d}) does not exist.'.format(host_user_id))

        now = int(time.mktime(time.gmtime()))
        yield self.r.call('HMSET', self.prefix+GAMEPREFIX+str(game_id),
                'date_created', now, GAME_DATA_KEY, '')

        # Redis stores the id as a string, so we have to convert to an int
        raise gen.Return(int(game_id))


    @gen.coroutine
    def store_game(self, game_id, encoded_game):
        """Store a Game object encoded as a string. Raise GTRDBError if an error occurs.
        """
        res = yield self.r.call('HSET', self.prefix+GAMEPREFIX+str(game_id),
                GAME_DATA_KEY, encoded_game)

        if isinstance(res, TornadisException):
            raise GTRDBError('Failed to store game {0!s}: "{1}"'
                    .format(game_id, res.message))


    @gen.coroutine
    def retrieve_game(self, game_id):
        """Retrieve a game, returning the encoded bytestring.

        Raise GTRDBError if the game does not exist or if there is an error
        communicating with the database.
        """
        encoded_game = yield self.r.call('HGET',
                self.prefix+GAMEPREFIX+str(game_id), GAME_DATA_KEY)
        if isinstance(encoded_game, TornadisException):
            raise GTRDBError('Failed to retrieve game {0!s}: "{1}"'
                    .format(encoded_game.message))
        elif encoded_game is None:
            raise GTRDBError('Game {0!s} does not exist.'.format(game_id))

        raise gen.Return(encoded_game)


    @gen.coroutine
    def retrieve_games(self, game_ids):
        """Return a list of games as the JSON-encoding of the Game object.

        If a game doesn't exist, None will be returned in its place.
        """
        if len(game_ids) == 0:
            raise gen.Return([])
        else:
            pipeline = tornadis.Pipeline()

            for game_id in game_ids:
                pipeline.stack_call('HGET',
                        self.prefix+GAMEPREFIX+str(game_id), GAME_DATA_KEY)

            pipeline = yield self.r.call(pipeline)

            raise gen.Return(pipeline)


    @gen.coroutine
    def retrieve_games_hosted_by_user(self, user_id):
        """Get list of game_ids hosted by user with ID user_id.
        """
        game_ids = yield self.r.call('LRANGE', self.prefix+GAMES_HOSTED_PREFIX+str(user_id), 0, -1)
        if isinstance(game_ids, TornadisException):
            raise GTRDBError('Failed to retrieve games hosted by user: {0}'
                    .format(user_id))
        else:
            raise gen.Return(map(int,game_ids))


    @gen.coroutine
    def retrieve_latest_games(self, n_games):
        """Get the n_games most recently-created games.
        """
        game_ids = yield self.r.call('LRANGE', self.prefix+GAMES, 0, n_games)
        raise gen.Return(map(int,game_ids))

    
    @gen.coroutine
    def add_user(self, username, auth_token):
        """Add a new user with auth_token. Sets the date_added timestamp.
        Auth token is stored in plaintext, so it should be hashed before
        providing it to this function.
        """
        # This check leaves open the possibility that the user is separately
        # registered between the check and the registration, but the register
        # function checks this atomically.
        exists = yield self.r.call('HEXISTS', self.prefix+USERNAMES, username)
        if isinstance(exists, TornadisException):
            raise GTRDBError('Error communicating with database: {0}'
                    .format(exists.message))
        elif exists:
            user_id = yield self.r.call('HGET', self.prefix+USERNAMES, username)
            if user_id is not None:
                raise GTRDBError('User {0} already exists with user ID {1}'
                        .format(username, user_id))
            else:
                raise GTRDBError('User {0} exists, but no user ID found.'
                        .format(username))

        user_id = yield self.register_user(username)
        unix_time_utc = int(time.mktime(time.gmtime()))

        result = yield self.r.call('HMSET', self.prefix+USERPREFIX+str(user_id),
                'date_added', unix_time_utc,
                'last_login', unix_time_utc,
                'auth', auth_token)

        if isinstance(result, TornadisException):
            raise GTRDBError(result.message)

        # register_user already converts to an int, so we can just return here.
        raise gen.Return(user_id)


    @gen.coroutine
    def register_user(self, username):
        """Evaluate the register_user lua script. Return the new user ID as an
        integer.

        The register_user script does the following:
            1) Check if the username exists and return None if it does.
            2) Increment the last-used user ID. Set this as the new user's ID.
            3) Set a hash for the user ID with one field "username" equal to
            <username>.
            4) Set the reverse-mapping from username to user ID.
            5) Return the user id.

        This function calls the script and checks the return/exception, 
        raising a GTRDBError if the username exists or if registering
        fails for another reason.
        """
        result = yield self.r.call('EVALSHA', self.scripts_sha['register'],
                2, self.prefix+USERID, self.prefix+USERNAMES, username)

        if isinstance(result, TornadisException):
            if result.message.startswith('NOSCRIPT'):
                result = yield self.r.call('EVAL', lua_scripts.REGISTER_USER,
                        2, self.prefix+USERID, self.prefix+USERNAMES, username)
            else:
                raise GTRDBError('Failed to register new user {0}: {1}'
                        .format(username, result.message))

        if result is None:
            user_id = yield self.r.call('HGET', self.prefix+USERNAMES, username)
            if user_id is not None:
                raise GTRDBError('User {0} already exists with user ID {1}'
                        .format(username, user_id))

            raise GTRDBError('Failed to register new user {0}'
                    .format(username))
        else:
            # User ids should be integers
            raise gen.Return(int(result))


    @gen.coroutine
    def update_user_last_login(self, user_id, last_login_time):
        """Updates <last_login> field of user hash to last_login_time,
        represented as integer seconds since the UNIX epoch, UTC.

        Raise GTRDBError if user doesn't exist.
        """
        yield self.r.call('HSET', self.prefix+USERPREFIX+str(user_id), 'last_login', last_login_time)


    @gen.coroutine
    def retrieve_user_id_from_username(self, username):
        """Get user_id from username by examining the "users" table.
        Return None if the username is not found.
        """
        user_id = yield self.r.call('HGET', self.prefix+USERNAMES, username)
        if isinstance(user_id, TornadisException): 
            raise GTRDBError('Failed to get user ID for {0}: {1}'
                    .format(username, user_id.message))
        else:
            raise gen.Return(int(user_id))


    @gen.coroutine
    def retrieve_user_auth(self, user_id):
        user_auth = yield self.r.call('HGET', self.prefix+USERPREFIX+str(user_id), 'auth')
        if isinstance(user_auth, TornadisException):
            raise GTRDBError('Failed to get user auth for {0}: {1}'
                    .format(username, res.message))
        else:
            raise gen.Return(user_auth)


    @gen.coroutine
    def retrieve_user_session_auth(self, user_id):
        session_auth = yield self.r.call(
                'HGET', self.prefix+USERPREFIX+str(user_id), 'session_auth')
        if isinstance(session_auth, TornadisException):
            raise GTRDBError('Failed to get session token for {0}: {1}'
                    .format(username, session_auth.message))
        else:
            raise gen.Return(session_auth)


    @gen.coroutine
    def retrieve_userid_from_session_auth(self, session_auth):
        user_id = yield self.r.call('HGET', self.prefix+SESSIONS, session_auth)
        # Redis values are strings, must convert to str
        raise gen.Return(int(user_id))


    @gen.coroutine
    def retrieve_user(self, user_id):
        """Return the entire User dictionary, formatted as a Python dict.
        """
        res = yield self.r.call('HGETALL', self.prefix+USERPREFIX+str(user_id))
        if isinstance(res, TornadisException):
            raise GTRDBError('Failed to retrieve user {0!s}: {1}'
                    .format(user_id, res.message))
        else:
            # tornadis gives us hashes as lists, with alternating
            # key1, value1, key2, value2, etc.
            # This formats as a Python dict.
            user_dict = dict(zip(res[::2], res[1::2]))
            raise gen.Return(user_dict)


    @gen.coroutine
    def update_user_session(self, user_id, session_auth):
        """Replaces a session token for user.
        """
        old_session_auth = yield self.r.call(
                'HGET', self.prefix+USERPREFIX+str(user_id), 'session_auth')

        pipeline = tornadis.Pipeline()
        pipeline.stack_call('HSET', self.prefix+USERPREFIX+str(user_id),
                'session_auth', session_auth)

        if old_session_auth is not None:
            pipeline.stack_call('HDEL', self.prefix+SESSIONS, old_session_auth)

        pipeline.stack_call('HSET', self.prefix+SESSIONS, session_auth, user_id)
        res = yield self.r.call(pipeline)

    @gen.coroutine
    def set_game_action(self, game_id, action_number, action_encoded):
        """Set `action_encoded` for given action number for game with id
        `game_id`. This will overwrite another action if it exists.
        """
        res = yield self.r.call('HSET', self.prefix+GAME_MOVE_PREFIX+str(game_id),
                action_number, action_encoded)

        if isinstance(res, TornadisException):
            raise GTRDBError('Failed to set game action {0:d}:{1:d}: {2}'
                    .format(game_id, action_number, action_encoded))


    @gen.coroutine
    def retrieve_game_actions(self, game_id, action_numbers):
        """Return the encoded actions (as strings) for the specified game for
        each action number in the sequence `action_numbers`.
        """
        fields = map(str, action_numbers)
        res = yield res.r.call('HMGET', self.prefix+GAME_MOVE_PREFIX+str(game_id),
                *fields)

        if isinstance(res, TornadisException):
            raise GTRDBError('Failed to retrieve game actions {0:d}:{1!s}'
                    .format(game_id, action_numbers))
        else:
            raise gen.Return(res)
