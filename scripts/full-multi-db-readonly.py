#!/usr/bin/env python3
"""Restrict four pre-seeded bookapp users to SELECT on bookdb.books.

Applies only after the seeded databases exist; preserves root credentials and PVCs.
Verifies no role/global/schema-wide/write grants are left. No default action.
"""
import argparse
import subprocess
import sys

NAMESPACE = 'full-multi'

class Refusal(RuntimeError): pass

def sql(pod, query, root=False):
    pwd='$MYSQL_ROOT_PASSWORD' if root else '$MYSQL_PASSWORD'
    user='root' if root else '$MYSQL_USER'
    # The SQL is passed as a positional parameter, not evaluated as shell code.
    shell=f'set -eu; MYSQL_PWD="{pwd}" mysql -u "{user}" --database="$MYSQL_DATABASE" --default-character-set=utf8mb4 -N -B -e "$1"'
    command=['oc','-n',NAMESPACE,'exec',pod,'-c','mariadb','--',
             'sh','-c',shell,'sh',query]
    r=subprocess.run(command, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise Refusal(f'{pod}: failed MariaDB SQL command: {r.stderr.strip()}')
    return r.stdout.strip().splitlines()

def verify(pod):
    grants=sql(pod,'SHOW GRANTS; SELECT COUNT(*) FROM books;')
    if not grants or grants[-1]!='20':
        raise Refusal(f'{pod}: expected exactly 20 books, got {grants[-1:]!r}')
    granted=grants[:-1]
    if len(granted)!=2 or not any(x.startswith('GRANT USAGE ON *.* TO ') for x in granted) or not any(x.startswith('GRANT SELECT ON `bookdb`.`books` TO ') for x in granted):
        raise Refusal(f'{pod}: unexpected application privileges: {granted!r}')
    print(f'PASS {pod}: 20 books, application identity has table-scoped SELECT only', flush=True)

def apply(pod):
    accounts=sql(pod,"SELECT Host FROM mysql.user WHERE User='bookapp' ORDER BY Host;",root=True)
    if accounts!=['%']:
        raise Refusal(f'{pod}: unexpected bookapp accounts {accounts!r}; no modification')
    sql(pod,"REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'bookapp'@'%'; GRANT SELECT ON bookdb.books TO 'bookapp'@'%';",root=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    x=parser.add_mutually_exclusive_group(required=True)
    x.add_argument('--apply',action='store_true')
    x.add_argument('--verify',action='store_true')
    args=parser.parse_args()
    try:
        for app in 'ab':
            for track in ('blue','green'):
                pod=f'mariadb-{app}-{track}-0'
                if args.apply:apply(pod)
                verify(pod)
    except (Refusal,subprocess.TimeoutExpired) as exc:
        print('ERROR:',exc,file=sys.stderr)
        sys.exit(1)
