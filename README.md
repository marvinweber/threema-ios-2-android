# Threema iOS to Android Migration

This python tool/script can be used to migrate an iOS Threema database to Android.
Even though the script takes over the "heavy" work, it still requires some manual
actions.

## What is migrated
- Contacts (incl. profile pictures)
- Groups (incl. groups you created yourself, group pictures, left/kicked state)
- Private chats and group chats
- Text messages (incl. quotes, edit/delete timestamps, delivery/read state)
- Images, videos, voice messages and files (with thumbnails and captions)
- Locations
- Polls (converted to a text message with title and options)

Not migrated: system/status messages (e.g. "X left the group", calls),
distribution lists, reactions.

Media that was never downloaded on the iPhone (or was deleted there) is not in
the backup; such messages are restored as "not downloaded" placeholders.

The script writes the Android data backup format **version 27**.

## Procedure

### Create unencrypted iOS Backup with iTunes
Use iTunes to create an **unencrypted** backup of your device, where Threema is
installed on.

### Extract Threema Database and Files from the Backup
Use a tool like [iBackup Viewer](https://www.imactools.com/iphonebackupviewer/)
to extract the required files. This tool is free for unencrypted backups.  
You find them in the following "directory": **AppDomainGroup-group.ch.threema** and
from there you need the `ThreemaData.sqlite` file (plus `ThreemaData.sqlite-wal`
and `-shm` if present – they contain the newest messages) and the `.ThreemaData_SUPPORT`
directory.  
Save both files somewhere on your disk and use the path to the sqlite file as first
argument for the python script.

### Encrypted iOS backups
If the backup is encrypted (e.g. enforced by MDM), extract the Threema files with
`src/extract_ios_backup.py` (needs `pip install iphone_backup_decrypt`):

```
python3 src/extract_ios_backup.py <backup-dir>/<UDID> ./threema-ios
```

This works with backups created by iTunes/Finder or `idevicebackup2` (libimobiledevice, Linux).

### Run the Python Script
Keep `ThreemaData.sqlite` and `.ThreemaData_SUPPORT` in the same directory (the
media files are in `.ThreemaData_SUPPORT/_EXTERNAL_DATA`; otherwise pass
`--support-dir`).

```
python3 src/main.py /path/to/ThreemaData.sqlite ./output
```

Options:
- `--my-identity ABCD1234` – your own Threema ID (auto-detected from groups)
- `--no-media` – export text only
- `--android-backup <zip> --password <pw> [--target <zip>]` – merge directly
  into the Android backup and write a new encrypted ZIP (needs
  `pip install pyzipper`). This replaces the manual "Migration" step below.

At the end a summary shows how many messages and media files were exported and
what was skipped.

### Create Android Backup
Configure Threema with the same ID (!) on your (new) Android device (you can use
Threema Safe to transfer your ID (without chats) from iOS to Android).  
Then, create a backup within Android. Copy it to your pc and un-zip it with the
password you set during the backup configuration.

### Migration
Easiest: use `--android-backup` and `--password` (see above), then copy the
resulting ZIP to your phone.

Manually: un-zip the Android backup, delete all `*.csv` files, media files and
`settings` in it **except** `identity`, `nonces*.csv` and `nonce_counts.csv`, then
copy everything from the `output` folder into it. Zip the content (not the
directory) again with AES-256 and **the same password** as the original backup
(your Threema ID inside `identity` is encrypted with it).

### Import Backup
On your Android device reset Threema again and start configuring it again. Upon
first start, select to restore backup and select the backup zip file you created
in the previous steps.

