"""Conservative, staging-only MySQL import of a limited Aurora mysqldump subset.

This is not a general-purpose SQL interpreter. Reject unfamiliar dump syntax.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import subprocess

from aurora.database.cli_inspection import load_cli_database_config
from aurora.database.cli_mysql_backup import _run
from aurora.database.cli_mysql_restore_preflight import _backup_file, _IDENTIFIER


@dataclass(frozen=True)
class StagingPlan:
    source_database: str
    staging_database: str
    backup: Path
    digest: str
    tables: int
    statements: int


def _parse_values(values: str) -> int:
    """Accept only literal tuples; reject functions, variables and SQL expressions."""
    token = re.compile(r"(?:NULL|TRUE|FALSE|[-+]?(?:[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?|\.[0-9]+(?:[eE][-+]?[0-9]+)?))", re.I)
    index = 0
    length = len(values)
    rows = 0

    def skip_space(pos):
        while pos < length and values[pos].isspace():
            pos += 1
        return pos

    index = skip_space(index)
    while index < length:
        if values[index] != '(':
            raise ValueError('Unsupported INSERT values: expected tuple')
        index += 1
        fields = 0
        while True:
            index = skip_space(index)
            if index >= length:
                raise ValueError('Unterminated INSERT tuple')
            if values[index] == "'":
                index += 1
                while index < length:
                    if values[index] == "\\":
                        index += 2
                    elif values[index] == "'":
                        if index + 1 < length and values[index + 1] == "'":
                            index += 2
                        else:
                            index += 1
                            break
                    else:
                        index += 1
                else:
                    raise ValueError('Unterminated INSERT string')
            else:
                match = token.match(values, index)
                if not match:
                    raise ValueError('Unsupported INSERT expression')
                index = match.end()
            fields += 1
            index = skip_space(index)
            if index >= length:
                raise ValueError('Unterminated INSERT tuple')
            if values[index] == ')':
                index += 1
                break
            if values[index] != ',':
                raise ValueError('Unsupported INSERT expression')
            index += 1
        if not fields:
            raise ValueError('Empty INSERT tuple')
        rows += 1
        index = skip_space(index)
        if index == length:
            return rows
        if values[index] != ',':
            raise ValueError('Unsupported INSERT separator')
        index = skip_space(index + 1)
    raise ValueError('Empty INSERT values')


def _safe_values(values: str) -> bool:
    try:
        return _parse_values(values) > 0
    except ValueError:
        return False


def _expected_rows(data: bytes) -> dict[str, int]:
    """Count literal tuples in a previously validated dump."""
    counts: dict[str, int] = {}
    for line in data.decode('utf-8-sig').splitlines():
        match = re.fullmatch(r'INSERT INTO `([A-Za-z0-9_]+)` VALUES (.+);', line.strip())
        if match:
            counts[match.group(1)] = counts.get(match.group(1), 0) + _parse_values(match.group(2))
    return counts


# These are complete, exact session directives emitted by our supported
# mysqldump format. Never permit arbitrary SQL inside versioned comments.
_SESSION_DIRECTIVES = frozenset({
    '/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;',
    '/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;',
    '/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;',
    '/*!50503 SET NAMES utf8mb4 */;',
    '/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;',
    "/*!40103 SET TIME_ZONE='+00:00' */;",
    '/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;',
    '/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;',
    "/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;",
    '/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;',
    '/*!40101 SET @saved_cs_client     = @@character_set_client */;',
    '/*!50503 SET character_set_client = utf8mb4 */;',
    '/*!40101 SET character_set_client = @saved_cs_client */;',
    '/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;',
    '/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;',
    '/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;',
    '/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;',
    '/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;',
    '/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;',
    '/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;',
    '/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;',
})


def _allowed_versioned_comment(line: str) -> bool:
    if line in _SESSION_DIRECTIVES:
        return True
    # InnoDB key hints are accepted only for simple unqualified table names.
    return bool(re.fullmatch(
        r'/\*!40000 ALTER TABLE `[A-Za-z0-9_]+` (?:DISABLE|ENABLE) KEYS \*/;',
        line,
    ))


def _validate_dump(data: bytes) -> tuple[int, int]:
    """Permit a small, single-schema, table/data-only mysqldump dialect.

    Reject routines, triggers, views, events, database switches, executable
    version comments outside a tiny allowlist, and unrecognized statements.
    """
    if len(data) > 128 * 1024 * 1024 or b'\x00' in data:
        raise ValueError('Unsupported MySQL dump size or binary content')
    try:
        sql = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('MySQL staging restore requires UTF-8 SQL') from None
    if not sql.startswith('-- MySQL dump') or '-- Dump completed on' not in sql.splitlines()[-1]:
        raise ValueError('MySQL dump header/footer missing')
    tables: set[str] = set()
    inserts = 0
    statements = 0
    in_create = False
    current = None
    for line_number, original in enumerate(sql.splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith('--'):
            continue
        if line.startswith('/*'):
            if not _allowed_versioned_comment(line):
                raise ValueError(
                    f'Unsupported executable MySQL dump comment at line {line_number}'
                )
            statements += 1
            continue
        if in_create:
            if re.fullmatch(r'\) ENGINE=InnoDB(?: [A-Za-z0-9_=., ]+)?;', line, re.I):
                in_create = False
                statements += 1
            elif re.fullmatch(r'`[A-Za-z0-9_]+` (?:tinyint|smallint|mediumint|int|integer|bigint|varchar\([0-9]+\)|char\([0-9]+\)|text|longtext|datetime|timestamp|date|decimal\([0-9]+,[0-9]+\)|float|double|blob|boolean)(?:\s+(?:unsigned|NOT NULL|NULL|AUTO_INCREMENT|DEFAULT (?:NULL|[0-9]+|CURRENT_TIMESTAMP|\x27[^\x27]*\x27)|COMMENT \x27[^\x27]*\x27))*[,]?', line, re.I):
                continue
            elif re.fullmatch(r'(?:PRIMARY KEY|UNIQUE KEY `[A-Za-z0-9_]+`|KEY `[A-Za-z0-9_]+`) \(`[A-Za-z0-9_]+`(?:,`[A-Za-z0-9_]+`)*\),?', line, re.I):
                continue
            else:
                raise ValueError('Unsupported CREATE TABLE definition in dump')
            continue
        match = re.fullmatch(r'CREATE TABLE `([A-Za-z0-9_]+)` \(', line)
        if match:
            current = match.group(1)
            if current in tables:
                raise ValueError('Duplicate table definition in dump')
            tables.add(current)
            in_create = True
            continue
        match = re.fullmatch(r'DROP TABLE IF EXISTS `([A-Za-z0-9_]+)`;', line)
        if match:
            statements += 1
            continue
        match = re.fullmatch(r'INSERT INTO `([A-Za-z0-9_]+)` VALUES (.+);', line)
        if match and match.group(1) in tables and _safe_values(match.group(2)):
            # Values must be parsed by MySQL; a quoted string can contain SQL
            # punctuation, so do not attempt unsafe string substitutions.
            inserts += 1
            statements += 1
            continue
        raise ValueError('Unsupported SQL statement in staging dump')
    if in_create or not tables or not inserts:
        raise ValueError('Dump requires completed InnoDB tables and data inserts')
    return len(tables), statements


def _identity(command: list[str], database: str, username: str, label: str) -> str:
    result = _run(['mysql', *command, '--batch', '--skip-column-names', database,
                   '--execute=SELECT DATABASE(), CURRENT_USER()'], label=label)
    fields = result.stdout.strip().split('\t')
    if len(fields) != 2 or fields[0] != database or fields[1].split('@')[0] != username:
        raise ValueError('MySQL account/database identity mismatch')
    return fields[1]


def _stage_grants(connection: list[str], stage: str, account: str) -> None:
    result = _run(['mysql', *connection, '--batch', '--skip-column-names', stage,
                   '--execute=SHOW GRANTS'], label='staging privilege inspection')
    grants = result.stdout.splitlines()
    if not grants:
        raise ValueError('Staging grants could not be verified')
    required = {'SELECT', 'INSERT', 'CREATE', 'DROP', 'ALTER'}
    allowed = {'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP', 'INDEX',
               'ALTER', 'CREATE VIEW', 'SHOW VIEW', 'TRIGGER'}
    present: set[str] = set()
    # SHOW GRANTS is security-sensitive: accept only the exact authenticated
    # account and simple database-scoped privileges. Reject grant options,
    # roles, partial grants, unexpected clauses, and other schemas.
    if not re.fullmatch(r"[A-Za-z0-9_]+@[A-Za-z0-9_.:%-]+", account):
        raise ValueError('Staging account identity cannot be verified')
    username, hostname = account.split('@', 1)
    principal = f'`{username}`@`{hostname}`'
    for line in grants:
        if line == f'GRANT USAGE ON *.* TO {principal}':
            continue
        match = re.fullmatch(
            r'GRANT ([A-Z ,]+) ON `([A-Za-z0-9_]+)`\.\* TO (.+)', line
        )
        if not match or match.group(2) != stage or match.group(3) != principal:
            raise ValueError('Staging account has non-staging or unrecognized grants')
        privileges = set(match.group(1).split(', '))
        if not privileges <= allowed:
            raise ValueError('Staging account has excessive privileges')
        present |= privileges
    if not required <= present:
        raise ValueError('Staging account lacks required import privileges')


def staging_restore(config_path: str, alias: str, source: str, login_path: str | None,
                    staging_database: str | None, staging_login_path: str | None,
                    *, check_only: bool, confirm=lambda _: False) -> StagingPlan:
    if not login_path or not _IDENTIFIER.fullmatch(login_path):
        raise ValueError('Source --login-path is required')
    if not staging_login_path or not _IDENTIFIER.fullmatch(staging_login_path) or staging_login_path == login_path:
        raise ValueError('A separate --staging-login-path is required')
    config, _ = load_cli_database_config(config_path)
    url = config.resolve(alias)
    target = url.database
    if url.drivername != 'mysql+pymysql' or not target or not _IDENTIFIER.fullmatch(target) or not url.username or not url.host:
        raise ValueError('Staging restore requires a configured MySQL source')
    stage = staging_database
    if not stage or not _IDENTIFIER.fullmatch(stage) or len(stage) > 64 or stage.lower() == target.lower():
        raise ValueError('Staging database must be a distinct simple identifier')
    backup, size = _backup_file(source)
    data = backup.read_bytes()
    tables, statements = _validate_dump(data)
    digest = sha256(data).hexdigest()
    plan = StagingPlan(target, stage, backup, digest, tables, statements)
    server = ['--protocol=TCP', f'--host={url.host}', f'--port={url.port or 3306}']
    source_conn = [f'--login-path={login_path}', *server, f'--user={url.username}']
    source_identity = _identity(source_conn, target, url.username, 'source identity inspection')
    # Staging profile supplies its own username; do not force source username.
    stage_conn = [f'--login-path={staging_login_path}', *server]
    stage_probe = _run(['mysql', *stage_conn, '--batch', '--skip-column-names', stage,
                        '--execute=SELECT DATABASE(), CURRENT_USER()'], label='staging identity inspection')
    fields = stage_probe.stdout.strip().split('\t')
    if len(fields) != 2 or fields[0] != stage or fields[1] == source_identity:
        raise ValueError('Staging database/account must differ from source')
    _stage_grants(stage_conn, stage, fields[1])

    def verify_empty():
        result = _run(['mysql', *stage_conn, '--batch', '--skip-column-names', stage,
                       '--execute=SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()'],
                      label='staging emptiness inspection')
        if result.stdout.strip() != '0':
            raise ValueError('Staging database is not empty; no import attempted')
        # A schema with routines or events is not empty for restore purposes.
        result = _run(['mysql', *stage_conn, '--batch', '--skip-column-names', stage,
                       '--execute=SELECT (SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = DATABASE()) + '
                       '(SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = DATABASE())'],
                      label='staging object inspection')
        if result.stdout.strip() != '0':
            raise ValueError('Staging database contains routines/events; no import attempted')

    verify_empty()
    if check_only:
        return plan
    if not confirm(plan):
        raise ValueError('Staging restore cancelled; no SQL imported')
    verify_empty()
    # Refuse a changed file after approval; do not open arbitrary new content.
    if backup.read_bytes() != data:
        raise ValueError('Backup changed during staging restore approval')
    try:
        completed = subprocess.run(['mysql', *stage_conn, '--binary-mode=OFF', stage],
                                   input=data, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Staging import failed; partial staging changes may remain. Do not retry without inspection') from None
    if completed.returncode:
        raise ValueError('Staging import failed; partial staging changes may remain. Do not retry without inspection')
    # A zero client exit code is not proof that all expected data arrived.
    # Check the table set and exact row counts before reporting success.
    try:
        expected = _expected_rows(data)
        result = _run(['mysql', *stage_conn, '--batch', '--skip-column-names', stage,
                       '--execute=SELECT TABLE_NAME FROM information_schema.TABLES '
                       'WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = \'BASE TABLE\''],
                      label='post-import table verification')
        actual = set(result.stdout.splitlines())
        if actual != set(expected):
            raise ValueError('Staging table set differs from dump')
        for table, count in expected.items():
            result = _run(['mysql', *stage_conn, '--batch', '--skip-column-names', stage,
                           f'--execute=SELECT COUNT(*) FROM `{table}`'],
                          label='post-import row verification')
            if result.stdout.strip() != str(count):
                raise ValueError(f'Staging row count differs for {table}')
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('Staging import verification failed; partial or unverified staging changes may remain. '
                         'Do not retry without inspection') from exc
    return plan
