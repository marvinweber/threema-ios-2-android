"""
Extract the Threema files from an (encrypted) iOS backup made with idevicebackup2.

Usage: python extract_ios_backup.py <backup-dir>/<UDID> <output-dir>
Needs: pip install iphone_backup_decrypt
"""
import getpass
import os
import shutil
import sys

from iphone_backup_decrypt import EncryptedBackup

b, out = sys.argv[1], sys.argv[2]
backup = EncryptedBackup(backup_directory=b, passphrase=getpass.getpass('iOS backup password: '))
tmp = out + '_raw'
backup.extract_files(relative_paths_like='%', domain_like='AppDomainGroup-group.ch.threema',
                     output_folder=tmp, preserve_folders=True)
n = 0
for root, dirs, files in os.walk(tmp):
    for name in files:
        src = os.path.join(root, name)
        rel = os.path.relpath(src, tmp)
        if name.startswith('ThreemaData.sqlite'):
            dst = os.path.join(out, name)
        elif '.ThreemaData_SUPPORT' + os.sep in rel:
            dst = os.path.join(out, '.ThreemaData_SUPPORT', rel.split('.ThreemaData_SUPPORT' + os.sep, 1)[1])
        else:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        n += 1
shutil.rmtree(tmp)
print(n, 'files copied')
