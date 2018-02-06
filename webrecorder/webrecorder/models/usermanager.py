import os
import re
import base64
import hashlib
import json
import redis
import requests

from datetime import datetime

from getpass import getpass
from string import ascii_lowercase as alpha

from bottle import template, request
from cork import AAAException

from webrecorder.webreccork import ValidationException

from webrecorder.models.base import BaseAccess
from webrecorder.models.user import User

from webrecorder.utils import load_wr_config
from webrecorder.webreccork import WebRecCork
from webrecorder.redisutils import RedisTable


# ============================================================================
class UserManager(object):
    USER_RX = re.compile(r'^[A-Za-z0-9][\w-]{2,30}$')

    RESTRICTED_NAMES = ['login', 'logout', 'user', 'admin', 'manager',
                        'guest', 'settings', 'profile', 'api', 'anon',
                        'anonymous', 'register', 'join', 'download', 'live', 'embed']

    PASS_RX = re.compile(r'^(?=.*[\d\W])(?=.*[a-z])(?=.*[A-Z]).{8,}$')

    def __init__(self, redis, cork, config):
        self.redis = redis
        self.cork = cork
        self.config = config

        self.default_coll = config['default_coll']

        self.temp_prefix = config['temp_prefix']

        mailing_list = os.environ.get('MAILING_LIST', '').lower()
        self.mailing_list = mailing_list in ('true', '1', 'yes')
        self.default_list_endpoint = os.environ.get('MAILING_LIST_ENDPOINT', '')
        self.list_key = os.environ.get('MAILING_LIST_KEY', '')
        self.list_removal_endpoint = os.path.expandvars(
                                        os.environ.get('MAILING_LIST_REMOVAL', ''))
        self.payload = os.environ.get('MAILING_LIST_PAYLOAD', '')
        self.remove_on_delete = (os.environ.get('REMOVE_ON_DELETE', '')
                                 in ('true', '1', 'yes'))

        # custom cork auth decorators
        self.admin_view = self.cork.make_auth_decorator(role='admin',
                                                        fixed_role=True,
                                                        fail_redirect='/_login')
        self.auth_view = self.cork.make_auth_decorator(role='archivist',
                                                       fail_redirect='/_login')
        self.beta_user = self.cork.make_auth_decorator(role='beta-archivist',
                                                       fail_redirect='/_login')

    def has_user_email(self, email):
        #TODO: implement a email table, if needed?
        all_users = RedisTable(self.redis, 'h:users')
        for n, userdata in all_users.items():
            if userdata['email_addr'] == email:
                return True

        return False

    def get_user_email(self, user):
        if not user:
            return ''
        all_users = self.get_users()
        userdata = all_users[user]
        if userdata:
            return userdata.get('email_addr', '')
        else:
            return ''

    def validate_user(self, user, email):
        if self._has_user(user):
            msg = 'User <b>{0}</b> already exists! Please choose a different username'
            msg = msg.format(user)
            raise ValidationException(msg)

        if not self.USER_RX.match(user) or user in self.RESTRICTED_NAMES:
            msg = 'The name <b>{0}</b> is not a valid username. Please choose a different username'
            msg = msg.format(user)
            raise ValidationException(msg)

        if self.has_user_email(email):
            msg = 'There is already an account for <b>{0}</b>. If you have trouble logging in, you may <a href="/_forgot"><b>reset the password</b></a>.'
            msg = msg.format(email)
            raise ValidationException(msg)

        return True

    def validate_password(self, password, confirm):
        if password != confirm:
            raise ValidationException('Passwords do not match!')

        if not self.PASS_RX.match(password):
            raise ValidationException('Please choose a different password')

        return True

    @property
    def access(self):
        return request['webrec.access']

    def update_password(self, curr_password, password, confirm):
        username = self.access.session_user.name
        if not self.cork.verify_password(username, curr_password):
            raise ValidationException('Incorrect Current Password')

        self.validate_password(password, confirm)

        self.cork.update_password(username, password)

    def is_valid_invite(self, invitekey):
        try:
            if not invitekey:
                return False

            key = base64.b64decode(invitekey.encode('utf-8')).decode('utf-8')
            key.split(':', 1)
            email, hash_ = key.split(':', 1)

            table = RedisTable(self.redis, 'h:invites')
            entry = table[email]

            if entry and entry.get('hash_') == hash_:
                return email
        except Exception as e:
            print(e)
            pass

        msg = 'Sorry, that is not a valid invite code. Please try again or request another invite'
        raise ValidationException(msg)

    def delete_invite(self, email):
        table = RedisTable(self.redis, 'h:invites')
        try:
            archive_invites = RedisTable(self.redis, 'h:arc_invites')
            archive_invites[email] = table[email]
        except:
            pass
        del table[email]

    def save_invite(self, email, name, desc=''):
        if not email or not name:
            return False

        table = RedisTable(self.redis, 'h:invites')
        table[email] = {'name': name, 'email': email, 'desc': desc}
        return True

    def send_invite(self, email, email_template, host):
        table = RedisTable(self.redis, 'h:invites')
        entry = table[email]
        if not entry:
            print('No Such Email In Invite List')
            return False

        hash_ = base64.b64encode(os.urandom(21)).decode('utf-8')
        entry['hash_'] = hash_

        full_hash = email + ':' + hash_
        invitekey = base64.b64encode(full_hash.encode('utf-8')).decode('utf-8')

        email_text = template(
            email_template,
            host=host,
            email_addr=email,
            name=entry.get('name', email),
            invite=invitekey,
        )
        self.cork.mailer.send_email(email, 'You are invited to join webrecorder.io beta!', email_text)
        entry['sent'] = str(datetime.utcnow())
        return True

    def add_to_mailing_list(self, username, email, name, list_endpoint=None):
        """3rd party mailing list subscription"""
        if not (list_endpoint or self.default_list_endpoint) or not self.list_key:
            print('MAILING_LIST is turned on, but required fields are '
                  'missing.')
            return

        # if no endpoint provided, use default
        if list_endpoint is None:
            list_endpoint = self.default_list_endpoint

        try:
            res = requests.post(list_endpoint,
                                auth=('nop', self.list_key),
                                data=self.payload.format(
                                    email=email,
                                    name=name,
                                    username=username),
                                timeout=1.5)

            if res.status_code != 200:
                print('Unexpected mailing list API response.. '
                      'status code: {0.status_code}\n'
                      'content: {0.content}'.format(res))

        except Exception as e:
            if e is requests.exceptions.Timeout:
                print('Mailing list API timed out..')
            else:
                print('Adding to mailing list failed:', e)

    def remove_from_mailing_list(self, email):
        """3rd party mailing list removal"""
        if not self.list_removal_endpoint or not self.list_key:
            # fail silently, log info
            print('REMOVE_ON_DELETE is turned on, but required '
                  'fields are missing.')
            return

        try:
            email = email.encode('utf-8').lower()
            email_hash = hashlib.md5(email).hexdigest()
            res = requests.delete(self.list_removal_endpoint.format(email_hash),
                                  auth=('nop', self.list_key),
                                  timeout=1.5)

            if res.status_code != 204:
                print('Unexpected mailing list API response.. '
                      'status code: {0.status_code}\n'
                      'content: {0.content}'.format(res))

        except Exception as e:
            if e is requests.exceptions.Timeout:
                print('Mailing list API timed out..')
            else:
                print('Removing from mailing list failed:', e)

    def get_session(self):
        return request.environ['webrec.session']

    def get_users(self):
        return RedisTable(self.redis, 'h:users')

    def get_user(self, username, access=BaseAccess()):
        user = User(my_id=username,
                    redis=self.redis,
                    access=access)

        return user

    def create_new_user(self, username, init_info=None):
        init_info = init_info or {}

        user = self.get_user(username)

        user.create_new()
        first_coll = None

        move_info = init_info.get('move_info')
        if move_info:
            first_coll = self.move_temp_coll(user, move_info)

        elif self.default_coll:
            first_coll = user.create_collection(self.default_coll['id'],
                                   title=self.default_coll['title'],
                                   desc=self.default_coll['title'].format(username),
                                   public=False)

        # email subscription set up?
        if self.mailing_list:
            self.add_to_mailing_list(username, init_info['email'], init_info.get('name', ''))

        return user, first_coll

    def create_user(self, reg):
        try:
            user, init_info = self.cork.validate_registration(reg)
        except AAAException as a:
            raise ValidationException(a)

        if init_info:
            init_info = json.loads(init_info)

        user, first_coll = self.create_new_user(user, init_info)

        self.cork.do_login(user.name)

        sesh = self.get_session()
        if not sesh.curr_user:
            sesh.curr_user = user.name

        return user, first_coll

    def has_space_for_new_collection(self, to_username, from_username, coll_name):
        to_user = self.get_user(to_username)
        if not self.is_valid_user(to_user):
            return False

        from_user = self.get_user(from_username)
        collection = from_user.get_collection_by_name(coll_name)
        if not collection:
            return False

        return (collection.size <= to_user.get_size_remaining())

    def move_temp_coll(self, user, move_info):
        from_user = self.get_user(move_info['from_user'])
        temp_coll = from_user.get_collection_by_name('temp')
        from_user.move(temp_coll, move_info['to_coll'], user)
        temp_coll.set_prop('title', move_info['to_title'])
        return temp_coll

    def is_valid_user(self, user):
        if user.is_anon():
            return True

        return self._has_user(user.name)

    def _has_user(self, username):
        return self.cork.user(username) is not None


# ============================================================================
class CLIUserManager(UserManager):
    def __init__(self):
        config = load_wr_config()

        # Init Redis
        redis_url = os.environ['REDIS_BASE_URL']

        r = redis.StrictRedis.from_url(redis_url, decode_responses=True)

        # Init Cork
        cork = WebRecCork.create_cork(r, config)

        super(CLIUserManager, self).__init__(
            redis=r,
            cork=cork,
            config=config)

    def create_user(self, email=None, username=None, passwd=None, role=None, name=None):
        """Create a new user with command line arguments or series of prompts,
           preforming basic validation
        """
        users = self.get_users()

        if not email:
            print('let\'s create a new user..')
            email = input('email: ').strip()

        # validate email
        if not re.match(r'[\w.-/+]+@[\w.-]+.\w+', email):
            print('valid email required!')
            return

        if email in [data['email_addr'] for u, data in users.items()]:
            print('A user already exists with {0} email!'.format(email))
            return

        username = username or input('username: ').strip()

        # validate username
        if not username:
            print('please enter a username!')
            return

        if not self.USER_RX.match(username) or username in self.RESTRICTED_NAMES:
            print('Invalid username..')
            return

        if username in users:
            print('Username already exists..')
            return

        name = name if name is not None else input('name (optional): ').strip()

        role = role if role in [r[0] for r in self.cork.list_roles()] else self.choose_role()

        if passwd is not None:
            passwd2 = passwd
        else:
            passwd = getpass('password: ')
            passwd2 = getpass('repeat password: ')

        if passwd != passwd2 or not self.PASS_RX.match(passwd):
            print('Passwords must match and be at least 8 characters long '
                         'with lowercase, uppercase, and either digits or symbols.')
            return

        print('Creating user {username} with the email {email} and the role: '
              '\'{role}\''.format(username=username,
                                  email=email,
                                  role=role))

        # add user to cork
        self.cork._store.users[username] = {
            'role': role,
            'hash': self.cork._hash(username, passwd).decode('ascii'),
            'email_addr': email,
            'desc': '{{"name":"{name}"}}'.format(name=name),
            'creation_date': str(datetime.utcnow()),
            'last_login': str(datetime.utcnow()),
        }
        self.cork._store.save_users()

        user, first_coll = self.create_new_user(username, {'email': email,
                                                           'name': name})

        print('All done!')
        return user, first_coll

    def choose_role(self):
        """Flexible choice prompt for as many roles as the system has"""
        roles = [r for r in self.cork.list_roles()]
        formatted = ['{0} (level {1})'.format(*r) for r in roles]
        condensed = '\n'.join(['{0}.) {1}'.format(*t) for t in zip(alpha, formatted)])
        new_role = input('choose: \n{0}\n\n'.format(condensed))

        if new_role not in alpha[:len(roles)]:
            raise Exception('invalid role choice')

        return roles[alpha.index(new_role)][0]

    def modify_user(self):
        """Modify an existing users. available modifications: role, email"""
        users = self.get_users()

        username = input('username to modify: ')
        has_modified = False

        if username not in users:
            print('{0} doesn\'t exist'.format(username))
            return

        mod_role = input('change role? currently {0} (y/n) '.format(users[username]['role']))
        if mod_role.strip().lower() == 'y':
            new_role = self.choose_role()
            self.cork._store.users[username]['role'] = new_role
            has_modified = True
            print('assigned {0} with the new role: {1}'.format(username, new_role))

        mod_email = input('update email? currently {0} (y/n) '.format(users[username]['email_addr']))
        if mod_email.strip().lower() == 'y':
            new_email = input('new email: ')

            if not re.match(r'[\w.-/+]+@[\w.-]+.\w+', new_email):
                print('valid email required!')
                return

            if new_email in [data['email_addr'] for u, data in users.items()]:
                print('A user already exists with {0} email!'.format(new_email))
                return

            # assume the 3rd party mailing list doesn't support updating addresses
            # so if add & remove are turned on, remove the old and add the
            # new address.
            if self.mailing_list and self.remove_on_delete:
                self.remove_from_mailing_list(users[username]['email_addr'])
                name = json.loads(self.get_users()[username].get('desc', '{}')).get('name', '')
                self.add_to_mailing_list(username, new_email, name)

            print('assigned {0} with the new email: {1}'.format(username, new_email))
            self.cork._store.users[username]['email_addr'] = new_email
            has_modified = True

        #
        # additional modifications can be added here
        #

        if has_modified:
            self.cork._store.save_users()

        print('All done!')

    def delete_user(self):
        """Remove a user from the system"""
        users = self.get_users()
        remove_on_delete = (os.environ.get('REMOVE_ON_DELETE', '')
                            in ('true', '1', 'yes'))

        username = input('username to delete: ')
        confirmation = input('** all data for the username `{0}` will be wiped! **\n'
                             'please type the username again to confirm: '.format(username))

        if username != confirmation:
            print('Username confirmation didn\'t match! Aborting..')
            return

        if username not in users:
            print('The username {0} doesn\'t exist..'.format(username))
            return

        print('removing {0}..'.format(username))

        # email subscription set up?
        if remove_on_delete:
            self.remove_from_mailing_list(users[username]['email_addr'])

        # delete user data and remove from redis
        # TODO: add tests
        u = self.get_user(username)
        u.delete_me()

        # delete user from cork
        self.cork.user(username).delete()




