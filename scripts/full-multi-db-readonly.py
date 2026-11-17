#!/usr/bin/env python3
"""Restrict four pre-seeded bookapp users to SELECT on bookdb.books.

Applies only after the seeded databases exist; preserves root credentials and PVCs.
Verifies no role/global/schema-wide/write grants are left. No default action.
"""
import argparse
import re
import subprocess
import sys

NAMESPACE = 'multi-ambient'
ACCOUNT = "'bookapp'@'%'"
TARGET = '`bookdb`.`books`'
TABLE_PRIVILEGES = {'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP',
                    'REFERENCES', 'INDEX', 'ALTER', 'CREATE VIEW', 'SHOW VIEW',
                    'TRIGGER', 'DELETE HISTORY', 'DELETE VERSIONING ROWS'}

class Refusal(RuntimeError): pass

def sql(pod, query, root=False):
    # The SCL MariaDB image supports passwordless local root socket access.
    # Application queries use their configured credentials.
    # The SQL is passed as a positional parameter, never evaluated as shell code.
    if root:
        shell = ('set -eu; unset MYSQL_PWD; '
                 'mysql --protocol=socket -u root '
                 '--database="$MYSQL_DATABASE" '
                 '--default-character-set=utf8mb4 -N -B -e "$1"')
    else:
        shell = ('set -eu; MYSQL_PWD="$MYSQL_PASSWORD" '
                 'mysql -u "$MYSQL_USER" '
                 '--database="$MYSQL_DATABASE" '
                 '--default-character-set=utf8mb4 -N -B -e "$1"')
    command=['oc','-n',NAMESPACE,'exec',pod,'-c','mariadb','--',
             'sh','-c',shell,'sh',query]
    r=subprocess.run(command, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise Refusal(f'{pod}: failed MariaDB SQL command: {r.stderr.strip()}')
    return r.stdout.strip().splitlines()

def select_only(granted):
    return (len(granted)==2 and
            not any('WITH GRANT OPTION' in x.upper() for x in granted) and
            any(x.startswith('GRANT USAGE ON *.* TO ') for x in granted) and
            any(x.startswith('GRANT SELECT ON `bookdb`.`books` TO ') for x in granted))

def verify(pod):
    grants=sql(pod,'SHOW GRANTS; SELECT COUNT(*) FROM books;')
    if not grants or grants[-1]!='20':
        raise Refusal(f'{pod}: expected exactly 20 books, got {grants[-1:]!r}')
    granted=grants[:-1]
    if not select_only(granted):
        raise Refusal(f'{pod}: unexpected application privileges; inspect SHOW GRANTS securely')
    print(f'PASS {pod}: 20 books, application identity has table-scoped SELECT only', flush=True)

def grant_scopes(grants):
    """Recognize simple account grants; refuse roles, proxies and complex objects."""
    identifier = r'`[A-Za-z0-9_$-]+`'
    scope = rf'(?:\*\.\*|{identifier}\.(?:\*|{identifier}))'
    account = r"(?:'bookapp'@'%'|`bookapp`@`%`)"
    result=[]
    for grant in grants:
        m=re.fullmatch(rf'GRANT ([A-Z ,]+) ON ({scope}) TO {account}(.*)', grant)
        if not m:
            raise Refusal('Unrecognized account grant (role, proxy, column or routine); inspect manually')
        privileges, target, suffix=m.groups()
        # Authentication clauses are emitted on the global grant. They are not
        # evaluated or reproduced. A grant option is the only suffix we change.
        if target!='*.*' and suffix.strip() not in ('', 'WITH GRANT OPTION'):
            raise Refusal('Unrecognized non-global grant options; inspect manually')
        result.append((target, privileges.strip(), 'WITH GRANT OPTION' in suffix.upper()))
    return result

def target_extras(pod):
    rows=sql(pod, "SELECT PRIVILEGE_TYPE FROM information_schema.TABLE_PRIVILEGES "
             "WHERE GRANTEE=CONCAT(CHAR(39),'bookapp',CHAR(39),'@',CHAR(39),'%',CHAR(39)) "
             "AND TABLE_SCHEMA='bookdb' AND TABLE_NAME='books' ORDER BY PRIVILEGE_TYPE;", root=True)
    found=set(rows)
    if 'SELECT' not in found or not found <= TABLE_PRIVILEGES:
        raise Refusal('Cannot establish the direct SELECT grant or an unrecognized table privilege is present')
    return sorted(found-{'SELECT'})

def apply(pod):
    accounts=sql(pod,"SELECT Host FROM mysql.user WHERE User='bookapp' ORDER BY Host;",root=True)
    if accounts!=['%']:
        raise Refusal(f'{pod}: unexpected bookapp accounts {accounts!r}; no modification')
    grants=sql(pod,"SHOW GRANTS FOR 'bookapp'@'%';",root=True)
    if select_only(grants):
        print(f'{pod}: SELECT-only grants already present; unchanged', flush=True)
        return
    grant_scopes(grants)  # Refuse unsupported drift before changing anything.
    sql(pod, f'GRANT SELECT ON {TARGET} TO {ACCOUNT};', root=True)
    if sql(pod, 'SELECT COUNT(*) FROM books;') != ['20']:
        raise Refusal(f'{pod}: application SELECT check failed; no privileges revoked')
    scopes=grant_scopes(sql(pod,"SHOW GRANTS FOR 'bookapp'@'%';",root=True))
    extras=target_extras(pod)
    for scope, privileges, grant_option in scopes:
        if grant_option:
            sql(pod, f'REVOKE GRANT OPTION ON {scope} FROM {ACCOUNT};', root=True)
        if scope==TARGET:
            if extras:
                sql(pod, f"REVOKE {', '.join(extras)} ON {TARGET} FROM {ACCOUNT};", root=True)
        elif privileges != 'USAGE':
            sql(pod, f'REVOKE ALL PRIVILEGES ON {scope} FROM {ACCOUNT};', root=True)

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
