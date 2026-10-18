"""
Threema iOS -> Android migration.

Reads an iOS Threema database (ThreemaData.sqlite + .ThreemaData_SUPPORT) and
writes the contents in the Android Threema data backup format (version 27):
contacts, groups, private and group chats including images, videos, voice
messages, files, locations, profile pictures and group pictures.
"""

import argparse
import csv
import datetime
import json
import os
import shutil
import sys
import uuid

from ios_db import IosThreemaDb, core_data_to_ms, to_hex

# Android data backup format version this script writes (RestoreSettings.CURRENT_VERSION)
BACKUP_FORMAT_VERSION = 27

# Android FileData rendering types
RENDERING_DEFAULT = 0
RENDERING_MEDIA = 1
RENDERING_STICKER = 2

CONTACT_COLUMNS = ['identity', 'publickey', 'verification', 'acid', 'firstname', 'lastname', 'nick_name',
                   'last_update', 'hidden', 'archived', 'identity_id']

GROUP_COLUMNS = ['id', 'creator', 'groupname', 'created_at', 'last_update', 'members', 'archived', 'groupDesc',
                 'groupDescTimestamp', 'group_uid', 'user_state']

MESSAGE_COLUMNS = ['apiid', 'uid', 'isoutbox', 'isread', 'issaved', 'messagestae', 'posted_at', 'created_at',
                   'modified_at', 'type', 'body', 'isstatusmessage', 'caption', 'quoted_message_apiid',
                   'delivered_at', 'read_at', 'g_msg_states', 'display_tags', 'edited_at', 'deleted_at']

GROUP_MESSAGE_COLUMNS = MESSAGE_COLUMNS[:2] + ['identity'] + MESSAGE_COLUMNS[2:]

# Files of the original Android backup that are kept when packaging
KEEP_FROM_ANDROID_BACKUP = ('identity', 'nonces.csv', 'nonces_d2d.csv', 'nonce_counts.csv', 'contact_avatar_me')

VERIFICATION_LEVELS = {0: 'UNVERIFIED', 1: 'SERVER_VERIFIED', 2: 'FULLY_VERIFIED'}

# iOS GroupEntity.GroupState -> Android UserState
GROUP_STATE_TO_USER_STATE = {0: 0, 1: 0, 2: 2, 3: 1}  # active/requestedSync -> MEMBER, left -> LEFT, forcedLeft -> KICKED


# --------------------------------------------------------------------- helpers

class AndroidCsv:
    """CSV writer matching Threema Android (opencsv, all fields quoted, backslash escaped)."""

    def __init__(self, path, columns):
        self.columns = columns
        self.file = open(path, 'w', encoding='utf-8', newline='')
        self.writer = csv.writer(self.file, quoting=csv.QUOTE_ALL, doublequote=True, lineterminator='\n')
        self.writer.writerow(columns)
        self.count = 0

    @staticmethod
    def _escape(value):
        if value is None:
            return ''
        if isinstance(value, bool):
            return '1' if value else '0'
        return str(value).replace('\\', '\\\\')

    def write(self, row: dict):
        self.writer.writerow([self._escape(row.get(c)) for c in self.columns])
        self.count += 1

    def close(self):
        self.file.close()


def sniff_mime(head: bytes, default='application/octet-stream'):
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', 'jpg'
    if head.startswith(b'\x89PNG'):
        return 'image/png', 'png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif', 'gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp', 'webp'
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in (b'heic', b'heix', b'mif1', b'msf1', b'hevc'):
            return 'image/heic', 'heic'
        if brand in (b'M4A ', b'M4B '):
            return 'audio/mp4', 'm4a'
        if brand == b'qt  ':
            return 'video/quicktime', 'mov'
        return 'video/mp4', 'mp4'
    if head.startswith(b'%PDF'):
        return 'application/pdf', 'pdf'
    if head.startswith(b'OggS'):
        return 'audio/ogg', 'ogg'
    if head.startswith(b'ID3') or head[:2] == b'\xff\xfb':
        return 'audio/mpeg', 'mp3'
    return default, None


def file_body(mime, size, file_name, rendering, downloaded, caption, thumb_mime, metadata):
    """Serialized FileDataModel (see FileDataModelSerializer.serializeFileDataBody)."""
    return json.dumps([
        None,  # blob id (not needed, file is included in backup)
        None,  # encryption key
        mime or 'application/octet-stream',
        int(min(size or 0, 2 ** 31 - 1)),
        file_name,
        int(rendering),
        bool(downloaded),
        caption,
        thumb_mime,
        metadata or {},
    ], ensure_ascii=False, separators=(',', ':'))


def timestamp_name(ms, prefix, ext):
    if ms:
        stamp = datetime.datetime.fromtimestamp(ms / 1000).strftime('%Y%m%d-%H%M%S')
    else:
        stamp = 'unknown'
    return '%s-%s.%s' % (prefix, stamp, ext) if ext else '%s-%s' % (prefix, stamp)


def num(value, default=None):
    if value is None or value == '':
        return default
    return value


# -------------------------------------------------------------------- exporter

class Exporter:

    def __init__(self, db: IosThreemaDb, output_dir, my_identity=None, include_media=True):
        self.db = db
        self.out = output_dir
        self.include_media = include_media
        self.my_identity = my_identity
        self.stats = {}
        self.media_bytes = 0

        # identity -> contact row info
        self.contacts = {}
        # contact pk -> identity
        self.contact_identity = {}

    def stat(self, key, inc=1):
        self.stats[key] = self.stats.get(key, 0) + inc

    # ------------------------------------------------------------ basics

    def run(self):
        os.makedirs(self.out, exist_ok=True)
        self._check_encryption()
        self._load_contacts()
        self._detect_my_identity()

        conversations = self._load_conversations()

        private_convs = [c for c in conversations if c['groupid'] is None and c['contact'] is not None
                         and not c['is_distribution_list']]
        group_convs = [c for c in conversations if c['groupid'] is not None]
        self.stat('skipped distribution lists', sum(1 for c in conversations if c['is_distribution_list']))

        contact_last_update = {}
        contact_archived = {}
        for conv in private_convs:
            identity = self.contact_identity.get(conv['contact'])
            if identity is None:
                continue
            last = self._export_private_chat(conv, identity)
            if last:
                contact_last_update[identity] = max(last, contact_last_update.get(identity, 0))
            if conv['archived']:
                contact_archived[identity] = True

        self._write_contacts(contact_last_update, contact_archived)
        self._export_groups(group_convs)
        self._write_settings()

    def _check_encryption(self):
        usage = self.db.encrypted_field_usage()
        if usage and usage[1] > 0 and usage[0] == 0:
            sys.exit('ERROR: The database content is encrypted ("Remote Secret", Threema Work). '
                     'It cannot be exported with this script.')
        if usage and usage[1] > 0:
            print('WARNING: %d text messages are only stored encrypted and will be skipped.' % usage[1])

    def _load_contacts(self):
        db = self.db
        c = lambda attr: db.column('ZCONTACT', attr)  # noqa: E731
        fields = {
            'identity': c('identity'),
            'publickey': c('publicKey'),
            'verification': c('verificationLevel'),
            'firstname': c('firstName'),
            'lastname': c('lastName'),
            'nick': c('publicNickName'),
            'hidden': c('hidden'),
            'image': c('imageData'),
        }
        select = ', '.join('"%s" AS %s' % (col, name) if col else 'NULL AS %s' % name
                           for name, col in fields.items())
        for row in db.conn.execute('SELECT Z_PK, %s FROM ZCONTACT' % select):
            identity = row['identity']
            if not identity or not row['publickey']:
                continue
            self.contact_identity[row['Z_PK']] = identity
            self.contacts[identity] = dict(row)

    def _detect_my_identity(self):
        if self.my_identity:
            return
        col = self.db.column('ZCONVERSATION', 'groupMyIdentity')
        if col:
            row = self.db.conn.execute(
                'SELECT "%s", count(*) AS n FROM ZCONVERSATION WHERE "%s" IS NOT NULL AND "%s" != \'\' '
                'GROUP BY 1 ORDER BY n DESC LIMIT 1' % (col, col, col)).fetchone()
            if row:
                self.my_identity = row[0]

    def _load_conversations(self):
        db = self.db
        c = lambda attr: db.column('ZCONVERSATION', attr)  # noqa: E731
        fields = {
            'contact': c('contact'),
            'groupid': c('groupId'),
            'groupname': c('groupName'),
            'groupmyidentity': c('groupMyIdentity'),
            'visibility': c('visibility'),
            'lastupdate': c('lastUpdate'),
            'groupimage': c('groupImage'),
            'distributionlist': c('distributionList'),
        }
        select = ', '.join('"%s" AS %s' % (col, name) if col else 'NULL AS %s' % name
                           for name, col in fields.items())
        conversations = []
        for row in db.conn.execute('SELECT Z_PK, %s FROM ZCONVERSATION' % select):
            conversations.append({
                'pk': row['Z_PK'],
                'contact': row['contact'],
                'groupid': to_hex(row['groupid']) if row['groupid'] is not None else None,
                'groupname': row['groupname'],
                'groupmyidentity': row['groupmyidentity'],
                'archived': row['visibility'] == 1,
                'lastupdate': core_data_to_ms(row['lastupdate']),
                'groupimage': row['groupimage'],
                'is_distribution_list': row['distributionlist'] is not None,
            })
        return conversations

    # ----------------------------------------------------------- contacts

    def _write_contacts(self, last_update, archived):
        out = AndroidCsv(os.path.join(self.out, 'contacts.csv'), CONTACT_COLUMNS)
        for identity, row in sorted(self.contacts.items()):
            if identity == self.my_identity:
                continue
            out.write({
                'identity': identity,
                'publickey': to_hex(row['publickey']),
                'verification': VERIFICATION_LEVELS.get(row['verification'], 'UNVERIFIED'),
                'acid': '',
                'firstname': row['firstname'],
                'lastname': row['lastname'],
                'nick_name': row['nick'],
                'last_update': last_update.get(identity),
                'hidden': bool(row['hidden']),
                'archived': archived.get(identity, False),
                'identity_id': identity,
            })
            if self.include_media and row['image']:
                ref = self.db.media_ref(row['image'])
                if ref:
                    self._write_media(ref, 'contact_profile_pic_' + identity)
                    self.stat('contact profile pictures')
        out.close()
        self.stat('contacts', out.count)

    # ------------------------------------------------------------- groups

    def _group_members(self):
        """conversation pk -> [identities], from the Core Data many-to-many table."""
        members = {}
        conv_pks = {r[0] for r in self.db.conn.execute('SELECT Z_PK FROM ZCONVERSATION')}
        for table, cols in self.db._columns.items():
            if not table.startswith('Z_') or len(cols) != 2:
                continue
            mcol = next((c for c in cols if c.endswith('MEMBERS')), None)
            gcol = next((c for c in cols if c.endswith('GROUPCONVERSATIONS')), None)
            if not mcol or not gcol:
                continue
            rows = self.db.conn.execute('SELECT "%s", "%s" FROM "%s"' % (mcol, gcol, table)).fetchall()
            # Core Data names the columns after the inverse relationship. Make sure we
            # use the column that actually references conversations.
            if rows and not all(r[1] in conv_pks for r in rows) and all(r[0] in conv_pks for r in rows):
                rows = [(r[1], r[0]) for r in rows]
            for contact_pk, conv_pk in rows:
                identity = self.contact_identity.get(contact_pk)
                if identity:
                    members.setdefault(conv_pk, []).append(identity)
        return members

    def _group_entities(self):
        """(groupid hex, creator or None) -> state, from the GroupEntity table."""
        result = {}
        if not self.db.has_table('ZGROUP'):
            return result
        gid = self.db.column('ZGROUP', 'groupId')
        creator = self.db.column('ZGROUP', 'groupCreator')
        state = self.db.column('ZGROUP', 'state')
        if not gid:
            return result
        sql = 'SELECT "%s", %s, %s FROM ZGROUP' % (
            gid, '"%s"' % creator if creator else 'NULL', '"%s"' % state if state else 'NULL')
        for g, c, s in self.db.conn.execute(sql):
            result[(to_hex(g), c or None)] = s
        return result

    def _export_groups(self, group_convs):
        if group_convs and not self.my_identity:
            sys.exit('ERROR: Could not detect your own Threema ID. Please pass it with --my-identity.')

        members = self._group_members()
        entities = self._group_entities()
        out = AndroidCsv(os.path.join(self.out, 'groups.csv'), GROUP_COLUMNS)
        seen = set()

        for conv in group_convs:
            my_identity = conv['groupmyidentity'] or self.my_identity
            contact_identity = self.contact_identity.get(conv['contact'])
            creator = contact_identity or my_identity
            group_uid = '%s-%s' % (conv['groupid'], creator)
            if group_uid in seen:
                self.stat('skipped duplicate groups')
                continue
            seen.add(group_uid)

            state = entities.get((conv['groupid'], contact_identity))
            if state is None:
                state = entities.get((conv['groupid'], creator))
            user_state = GROUP_STATE_TO_USER_STATE.get(state, 0)

            member_list = [my_identity, creator] + members.get(conv['pk'], [])
            if user_state != 0:
                member_list = [m for m in member_list if m != my_identity]
            member_list = list(dict.fromkeys(m for m in member_list if m))

            first, last = self._export_group_chat(conv, group_uid, my_identity)

            out.write({
                'id': conv['groupid'],
                'creator': creator,
                'groupname': conv['groupname'],
                'created_at': first or conv['lastupdate'] or 0,
                'last_update': last or conv['lastupdate'],
                'members': ';'.join(member_list),
                'archived': conv['archived'],
                'groupDesc': None,
                'groupDescTimestamp': None,
                'group_uid': group_uid,
                'user_state': user_state,
            })

            if self.include_media and conv['groupimage']:
                ref = self.db.data_entity_media('ZIMAGEDATA', conv['groupimage'])
                if ref:
                    self._write_media(ref, 'group_avatar_' + group_uid)
                    self.stat('group pictures')
        out.close()
        self.stat('groups', out.count)

    # ----------------------------------------------------------- messages

    def _messages(self, conv_pk):
        date_col = self.db.column('ZMESSAGE', 'date')
        conv_col = self.db.column('ZMESSAGE', 'conversation')
        return self.db.conn.execute(
            'SELECT * FROM ZMESSAGE WHERE "%s"=? ORDER BY "%s", Z_PK' % (conv_col, date_col), (conv_pk,))

    def _col(self, row, entity, attribute):
        col = self.db.column('ZMESSAGE', attribute, entity)
        return row[col] if col else None

    def _export_private_chat(self, conv, identity):
        print('Exporting private chat with', identity)
        out = AndroidCsv(os.path.join(self.out, 'message_%s.csv' % identity), MESSAGE_COLUMNS)
        last = None
        for row in self._messages(conv['pk']):
            converted = self._convert_message(row, 'message_')
            if converted is None:
                continue
            out.write(converted)
            last = max(last or 0, converted['posted_at'] or 0)
        out.close()
        self.stat('private messages', out.count)
        return last

    def _export_group_chat(self, conv, group_uid, my_identity):
        print('Exporting group chat', conv['groupname'] or group_uid)
        out = AndroidCsv(os.path.join(self.out, 'group_message_%s.csv' % group_uid), GROUP_MESSAGE_COLUMNS)
        first, last = None, None
        for row in self._messages(conv['pk']):
            converted = self._convert_message(row, 'group_message_')
            if converted is None:
                continue
            if converted['isoutbox']:
                converted['identity'] = my_identity
            else:
                sender = self._col(row, None, 'sender')
                converted['identity'] = self.contact_identity.get(sender)
                if not converted['identity']:
                    self.stat('skipped group messages without sender')
                    continue
            out.write(converted)
            ts = converted['posted_at'] or 0
            first = ts if first is None else min(first, ts)
            last = max(last or 0, ts)
        out.close()
        self.stat('group messages', out.count)
        return first, last

    def _convert_message(self, row, media_prefix):
        entity = self.db.entity_of(row['Z_ENT'])
        handler = {
            'TextMessage': self._text,
            'ImageMessage': self._image,
            'VideoMessage': self._video,
            'AudioMessage': self._audio,
            'FileMessage': self._file,
            'LocationMessage': self._location,
            'BallotMessage': self._ballot,
        }.get(entity)
        if handler is None:
            self.stat('skipped %s' % (entity or 'unknown messages'))
            return None

        date = core_data_to_ms(self._col(row, None, 'date'))
        if date is None:
            self.stat('skipped messages without date')
            return None

        uid = str(uuid.uuid4())
        content = handler(row, entity, uid, date)
        if content is None:
            return None
        msg_type, body, caption, media, thumb, quoted = content

        is_own = bool(self._col(row, None, 'isOwn'))
        delivered_at = core_data_to_ms(self._col(row, None, 'deliveryDate'))
        read_at = core_data_to_ms(self._col(row, None, 'readDate'))

        if is_own:
            if read_at:
                state = 'READ'
            elif delivered_at or self._col(row, None, 'delivered'):
                state = 'DELIVERED'
            elif self._col(row, None, 'sendFailed'):
                state = 'SENDFAILED'
            else:
                state = 'SENT'
        else:
            state = ''

        msg_id = to_hex(self._col(row, None, 'id'))
        if len(msg_id) != 16:
            msg_id = os.urandom(8).hex()

        if media is not None:
            self._write_media(media, media_prefix + 'media_' + uid)
        if thumb is not None:
            if thumb.size < 5 * 1024 * 1024:
                self._write_media(thumb, media_prefix + 'thumbnail_' + uid)

        return {
            'apiid': msg_id,
            'uid': uid,
            'isoutbox': is_own,
            'isread': True if is_own else bool(self._col(row, None, 'read')),
            'issaved': True,
            'messagestae': state,
            'posted_at': date,
            'created_at': date,
            'modified_at': max(x for x in (date, delivered_at, read_at) if x),
            'type': msg_type,
            'body': body,
            'isstatusmessage': False,
            'caption': caption,
            'quoted_message_apiid': quoted,
            'delivered_at': delivered_at,
            'read_at': read_at,
            'g_msg_states': '',
            'display_tags': 0,
            'edited_at': core_data_to_ms(self._col(row, None, 'lastEditedAt')),
            'deleted_at': core_data_to_ms(self._col(row, None, 'deletedAt')),
        }

    # ------------------------------------------------- message type handlers
    # each returns (type, body, caption, media_ref, thumbnail_ref, quoted_apiid) or None

    def _text(self, row, entity, uid, date):
        text = self._col(row, entity, 'text')
        if text is None:
            self.stat('skipped empty text messages')
            return None
        quoted = self._col(row, entity, 'quotedMessageId')
        self.stat('-> text')
        return 'TEXT', text, None, None, None, to_hex(quoted) or None

    def _thumbnail(self, row, entity):
        if not self.include_media:
            return None
        return self.db.data_entity_media('ZIMAGEDATA', self._col(row, entity, 'thumbnail'))

    def _media_or_missing(self, ref, kind):
        if ref is None:
            self.stat('missing %s data (not downloaded on iPhone)' % kind)
        else:
            self.stat('-> ' + kind)
        return ref

    def _image(self, row, entity, uid, date):
        image_pk = self._col(row, entity, 'image')
        media = self.db.data_entity_media('ZIMAGEDATA', image_pk) if self.include_media else None
        media = self._media_or_missing(media, 'images')
        mime, ext = sniff_mime(media.head(), 'image/jpeg') if media else ('image/jpeg', 'jpg')
        metadata = {}
        w, h = self.db.image_dimensions(image_pk)
        if w and h:
            metadata = {'w': int(w), 'h': int(h)}
        caption = self._col(row, entity, 'caption') if self.db.column('ZMESSAGE', 'caption', entity) else None
        body = file_body(mime, media.size if media else 0, timestamp_name(date, 'IMG', ext or 'jpg'),
                         RENDERING_MEDIA, media is not None, caption, 'image/jpeg', metadata)
        return 'FILE', body, caption, media, self._thumbnail(row, entity), None

    def _video(self, row, entity, uid, date):
        media = self.db.data_entity_media('ZVIDEODATA', self._col(row, entity, 'video')) \
            if self.include_media else None
        media = self._media_or_missing(media, 'videos')
        mime, ext = sniff_mime(media.head(), 'video/mp4') if media else ('video/mp4', 'mp4')
        duration = num(self._col(row, entity, 'duration'), 0)
        body = file_body(mime, media.size if media else num(self._col(row, entity, 'videoSize'), 0),
                         timestamp_name(date, 'VID', ext or 'mp4'), RENDERING_MEDIA, media is not None, None,
                         'image/jpeg', {'d': float(duration)})
        return 'FILE', body, None, media, self._thumbnail(row, entity), None

    def _audio(self, row, entity, uid, date):
        media = self.db.data_entity_media('ZAUDIODATA', self._col(row, entity, 'audio')) \
            if self.include_media else None
        media = self._media_or_missing(media, 'voice messages')
        duration = num(self._col(row, entity, 'duration'), 0)
        body = file_body('audio/aac', media.size if media else 0, timestamp_name(date, 'AUD', 'm4a'),
                         RENDERING_MEDIA, media is not None, None, None, {'d': float(duration)})
        return 'FILE', body, None, media, None, None

    def _file(self, row, entity, uid, date):
        media = self.db.data_entity_media('ZFILEDATA', self._col(row, entity, 'data')) \
            if self.include_media else None

        meta_json = {}
        raw_json = self._col(row, entity, 'json')
        if raw_json:
            try:
                meta_json = json.loads(raw_json)
            except (TypeError, ValueError):
                meta_json = {}
        if not isinstance(meta_json, dict):
            meta_json = {}

        mime = self._col(row, entity, 'mimeType') or meta_json.get('m')
        if not mime and media:
            mime = sniff_mime(media.head())[0]
        mime = mime or 'application/octet-stream'

        rendering = self._col(row, entity, 'type')
        if rendering is None:
            rendering = meta_json.get('j', RENDERING_DEFAULT)
        try:
            rendering = int(rendering)
        except (TypeError, ValueError):
            rendering = RENDERING_DEFAULT

        caption = self._col(row, entity, 'caption') or meta_json.get('d')
        file_name = self._col(row, entity, 'fileName') or meta_json.get('n') or timestamp_name(date, 'FILE', None)
        size = media.size if media else num(self._col(row, entity, 'fileSize'), 0)

        metadata = meta_json.get('x') if isinstance(meta_json.get('x'), dict) else {}
        metadata = {k: v for k, v in metadata.items() if isinstance(v, (str, int, float, bool))}

        kind = 'files'
        if mime.startswith('image/'):
            kind = 'images'
        elif mime.startswith('video/'):
            kind = 'videos'
        elif mime.startswith('audio/'):
            kind = 'voice messages' if rendering == RENDERING_MEDIA else 'audio files'
        self._media_or_missing(media, kind)

        thumb = self._thumbnail(row, entity)
        body = file_body(mime, size, file_name, rendering, media is not None, caption,
                         'image/jpeg' if thumb else None, metadata)
        return 'FILE', body, caption, media, thumb, None

    def _location(self, row, entity, uid, date):
        lat = self._col(row, entity, 'latitude')
        lng = self._col(row, entity, 'longitude')
        if lat is None or lng is None:
            self.stat('skipped locations without coordinates')
            return None
        accuracy = self._col(row, entity, 'accuracy')
        address = self._col(row, entity, 'poiAddress') or self._col(row, entity, 'reverseGeocodingResult')
        name = self._col(row, entity, 'poiName')
        body = json.dumps([float(lat), float(lng), float(accuracy) if accuracy is not None else None,
                           address or None, name or None], ensure_ascii=False, separators=(',', ':'))
        self.stat('-> locations')
        return 'LOCATION', body, None, None, None, None

    def _ballot(self, row, entity, uid, date):
        """Polls are converted to a text message (title + options), as Android polls can't be linked."""
        ballot_pk = self._col(row, entity, 'ballot')
        title, choices = None, []
        if ballot_pk is not None and self.db.has_table('ZBALLOT'):
            tcol = self.db.column('ZBALLOT', 'title')
            if tcol:
                r = self.db.conn.execute('SELECT "%s" FROM ZBALLOT WHERE Z_PK=?' % tcol, (ballot_pk,)).fetchone()
                title = r[0] if r else None
            if self.db.has_table('ZBALLOTCHOICE'):
                ncol = self.db.column('ZBALLOTCHOICE', 'name')
                bcol = self.db.column('ZBALLOTCHOICE', 'ballot')
                ocol = self.db.column('ZBALLOTCHOICE', 'orderPosition')
                if ncol and bcol:
                    choices = [r[0] for r in self.db.conn.execute(
                        'SELECT "%s" FROM ZBALLOTCHOICE WHERE "%s"=? ORDER BY %s' % (
                            ncol, bcol, '"%s"' % ocol if ocol else 'Z_PK'), (ballot_pk,)) if r[0]]
        text = '📊 Poll: %s' % (title or '?')
        if choices:
            text += '\n' + '\n'.join('• ' + c for c in choices)
        self.stat('-> polls (as text)')
        return 'TEXT', text, None, None, None, None

    # -------------------------------------------------------------- output

    def _write_media(self, ref, name):
        target = os.path.join(self.out, name)
        if ref.path:
            shutil.copyfile(ref.path, target)
        else:
            with open(target, 'wb') as f:
                f.write(ref.data)
        self.media_bytes += ref.size
        self.stat('media files written')

    def _write_settings(self):
        with open(os.path.join(self.out, 'settings'), 'w', encoding='utf-8', newline='') as f:
            csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator='\n').writerow(
                ['version', str(BACKUP_FORMAT_VERSION)])


# ------------------------------------------------------------------ packaging

def package_backup(output_dir, android_backup, password, target):
    try:
        import pyzipper
    except ImportError:
        sys.exit('ERROR: Packaging needs pyzipper: pip install pyzipper')

    pwd = password.encode('utf-8')
    with pyzipper.AESZipFile(android_backup) as src:
        src.setpassword(pwd)
        names = src.namelist()
        if 'identity' not in names:
            sys.exit('ERROR: %s does not look like a Threema Android data backup (no "identity").' % android_backup)
        for name in names:
            if name in KEEP_FROM_ANDROID_BACKUP:
                with src.open(name) as s, open(os.path.join(output_dir, name), 'wb') as d:
                    shutil.copyfileobj(s, d)

    with pyzipper.AESZipFile(target, 'w', compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as dst:
        dst.setpassword(pwd)
        dst.setencryption(pyzipper.WZ_AES, nbits=256)
        for name in sorted(os.listdir(output_dir)):
            path = os.path.join(output_dir, name)
            if not os.path.isfile(path) or name.startswith('.'):
                continue
            is_csv = name.endswith('.csv') or name in ('settings', 'identity')
            dst.write(path, arcname=name,
                      compress_type=pyzipper.ZIP_DEFLATED if is_csv else pyzipper.ZIP_STORED)
    print('Wrote encrypted Android backup:', target)


# ------------------------------------------------------------------------ main

def main():
    parser = argparse.ArgumentParser(description='Convert an iOS Threema database into an Android Threema backup.')
    parser.add_argument('database', help='path to ThreemaData.sqlite')
    parser.add_argument('output', nargs='?', default='./output', help='output directory (default: ./output)')
    parser.add_argument('--support-dir', help='path to .ThreemaData_SUPPORT (default: next to the database)')
    parser.add_argument('--my-identity', help='your own Threema ID (auto-detected from groups if omitted)')
    parser.add_argument('--no-media', action='store_true', help='skip images, videos, audio and files')
    parser.add_argument('--android-backup', help='Threema Android backup ZIP to merge into (optional)')
    parser.add_argument('--password', help='password of the Android backup (required with --android-backup)')
    parser.add_argument('--target', default='threema-backup-migrated.zip', help='output ZIP when packaging')
    args = parser.parse_args()

    if os.path.isdir(args.output) and os.listdir(args.output):
        sys.exit('ERROR: Output directory %s is not empty.' % args.output)
    if args.android_backup and not args.password:
        sys.exit('ERROR: --password is required with --android-backup')

    db = IosThreemaDb(args.database, args.support_dir)
    if not args.no_media and not os.path.isdir(db.external_dir):
        print('WARNING: %s not found - large media (images, videos, ...) will be missing.' % db.external_dir)

    exporter = Exporter(db, args.output, args.my_identity, include_media=not args.no_media)
    exporter.run()

    print()
    print('Summary')
    print('-------')
    for key in sorted(exporter.stats):
        print('  %-50s %d' % (key, exporter.stats[key]))
    if db.missing_external_files:
        print('  %-50s %d' % ('referenced files missing in _EXTERNAL_DATA', db.missing_external_files))
    print('  %-50s %.1f MB' % ('media size', exporter.media_bytes / 1024 / 1024))

    if args.android_backup:
        package_backup(args.output, args.android_backup, args.password, args.target)


if __name__ == '__main__':
    main()
