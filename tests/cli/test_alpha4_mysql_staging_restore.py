"""Guarded MySQL staging-only restore contract tests (no live database)."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from aurora.database.cli_mysql_staging_restore import staging_restore, _validate_dump

DUMP = b'''-- MySQL dump 10.13
/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
DROP TABLE IF EXISTS `recovery_items`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `recovery_items` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40000 ALTER TABLE `recovery_items` DISABLE KEYS */;
INSERT INTO `recovery_items` VALUES (1,'Aurora MVC'),(2,'MySQL Devil');
/*!40000 ALTER TABLE `recovery_items` ENABLE KEYS */;
-- Dump completed on 2026-10-28
'''


# Exact versioned-comment syntax observed in Aurora's live MySQL 8.0 dump.
REAL_DUMP_DIRECTIVES = [
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
    '/*!40000 ALTER TABLE `recovery_items` DISABLE KEYS */;',
    '/*!40000 ALTER TABLE `recovery_items` ENABLE KEYS */;',
    '/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;',
    '/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;',
    '/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;',
    '/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;',
    '/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;',
    '/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;',
    '/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;',
    '/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;',
]


def _realistic_dump():
    header = '-- MySQL dump 10.13\n' + '\n'.join(REAL_DUMP_DIRECTIVES[:10]) + '\n'
    body = DUMP.decode('utf-8').split('\n', 1)[1]
    body = body.replace('-- Dump completed on 2026-10-28',
                        '\n'.join(REAL_DUMP_DIRECTIVES[15:]) + '\n-- Dump completed on 2026-10-28')
    return (header + body).encode('utf-8')



class StagingRestoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'databases': {'default': {
            'url': 'mysql+pymysql://aurora_test@localhost:3306/aurora_recovery_test'}}}))
        self.dump = self.root / 'backup.sql'
        self.dump.write_bytes(DUMP)
        self.calls = []

    def fake_run(self, command, **kwargs):
        self.calls.append(command)
        sql = command[-1]
        if 'CURRENT_USER()' in sql:
            value = ('aurora_recovery_test\taurora_test@localhost\n'
                     if '--user=aurora_test' in command else
                     'aurora_restore_stage_round3\taurora_stage@localhost\n')
        elif 'SHOW GRANTS' in sql:
            value = ('GRANT USAGE ON *.* TO `aurora_stage`@`localhost`\n'
                     'GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER, CREATE VIEW, SHOW VIEW, TRIGGER '
                     'ON `aurora_restore_stage_round3`.* TO `aurora_stage`@`localhost`\n')
        elif 'TABLE_NAME FROM information_schema.TABLES' in sql:
            value = 'recovery_items\n'
        elif 'SELECT COUNT(*) FROM `recovery_items`' in sql:
            value = '2\n'
        else:
            value = '0\n'
        return subprocess.CompletedProcess(command, 0, value, '')

    def run_stage(self, **kwargs):
        with patch('aurora.database.cli_mysql_staging_restore._run', side_effect=self.fake_run):
            return staging_restore(str(self.config), 'default', str(self.dump), 'aurora_recovery',
                                   'aurora_restore_stage_round3', 'aurora_stage', **kwargs)

    def test_accepts_all_live_mysqldump_session_directives(self):
        tables, statements = _validate_dump(_realistic_dump())
        self.assertEqual(tables, 1)
        self.assertGreater(statements, 20)

    def test_rejects_modified_versioned_comment_with_line_number(self):
        dump = _realistic_dump().replace(
            b"/*!40103 SET TIME_ZONE='+00:00' */;",
            b"/*!40103 SET TIME_ZONE='+00:00'; DROP DATABASE x */;",
        )
        with self.assertRaisesRegex(ValueError, r'comment at line 7'):
            _validate_dump(dump)

    def test_rejects_unrecognized_session_setting(self):
        dump = _realistic_dump().replace(
            b'/*!50503 SET NAMES utf8mb4 */;',
            b'/*!50503 SET NAMES latin1 */;',
        )
        with self.assertRaisesRegex(ValueError, 'Unsupported executable'):
            _validate_dump(dump)

    def test_rejects_versioned_comment_with_other_table(self):
        dump = _realistic_dump().replace(
            b'/*!40000 ALTER TABLE `recovery_items` ENABLE KEYS */;',
            b'/*!40000 ALTER TABLE `other`.`recovery_items` ENABLE KEYS */;',
        )
        with self.assertRaisesRegex(ValueError, 'Unsupported executable'):
            _validate_dump(dump)

    def test_read_only_check(self):
        plan = self.run_stage(check_only=True)
        self.assertEqual(plan.tables, 1)
        self.assertTrue(all(command[0] == 'mysql' for command in self.calls))

    def test_refuses_same_login_profile(self):
        with self.assertRaisesRegex(ValueError, 'separate'):
            staging_restore(str(self.config), 'default', str(self.dump), 'aurora_stage',
                            'aurora_restore_stage_round3', 'aurora_stage', check_only=True)

    def test_refuses_source_as_stage(self):
        with self.assertRaisesRegex(ValueError, 'distinct'):
            staging_restore(str(self.config), 'default', str(self.dump), 'aurora_recovery',
                            'aurora_recovery_test', 'aurora_stage', check_only=True)

    def test_rejects_extra_statement(self):
        bad = DUMP.replace(b'-- Dump completed', b'DROP DATABASE `aurora_recovery_test`;\n-- Dump completed')
        with self.assertRaisesRegex(ValueError, 'Unsupported SQL'):
            _validate_dump(bad)

    def test_rejects_sql_injection_in_insert(self):
        bad = DUMP.replace(b"(2,'MySQL Devil');", b"(2,'MySQL Devil'); DROP DATABASE x;")
        with self.assertRaisesRegex(ValueError, 'Unsupported SQL'):
            _validate_dump(bad)

    def test_rejects_expressions_inside_insert(self):
        for expression in ('UUID()', '@session_value', '1+1', 'NOW()', 'SLEEP(1)'):
            with self.subTest(expression=expression):
                bad = DUMP.replace(b"(2,'MySQL Devil')", f"(2,{expression})".encode())
                with self.assertRaisesRegex(ValueError, 'Unsupported SQL'):
                    _validate_dump(bad)

    def test_accepts_literal_insert_values(self):
        dump = DUMP.replace(b"(2,'MySQL Devil')", b"(2,NULL),(3,'It\\'s safe'),(4,1.2e3)")
        self.assertEqual(_validate_dump(dump)[0], 1)

    def test_import_verifies_row_counts(self):
        with patch('aurora.database.cli_mysql_staging_restore.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, b'', b'')):
            def wrong_count(command, **kwargs):
                result = self.fake_run(command, **kwargs)
                if 'SELECT COUNT(*) FROM `recovery_items`' in command[-1]:
                    return subprocess.CompletedProcess(command, 0, '1\n', '')
                return result
            with patch('aurora.database.cli_mysql_staging_restore._run',
                       side_effect=wrong_count):
                with self.assertRaisesRegex(ValueError, 'verification failed'):
                    staging_restore(str(self.config), 'default', str(self.dump),
                                    'aurora_recovery', 'aurora_restore_stage_round3',
                                    'aurora_stage', check_only=False, confirm=lambda _: True)

    def test_rejects_routines(self):
        bad = DUMP.replace(b'-- Dump completed', b'CREATE PROCEDURE evil() SELECT 1;\n-- Dump completed')
        with self.assertRaisesRegex(ValueError, 'Unsupported SQL'):
            _validate_dump(bad)

    def test_refuses_nonempty_stage(self):
        def occupied(command, **kwargs):
            result = self.fake_run(command, **kwargs)
            if 'information_schema.TABLES' in command[-1]:
                return subprocess.CompletedProcess(command, 0, '1\n', '')
            return result
        with patch('aurora.database.cli_mysql_staging_restore._run', side_effect=occupied):
            with self.assertRaisesRegex(ValueError, 'not empty'):
                staging_restore(str(self.config), 'default', str(self.dump), 'aurora_recovery',
                                'aurora_restore_stage_round3', 'aurora_stage', check_only=True)

    def test_rejects_broad_grants(self):
        def broad(command, **kwargs):
            result = self.fake_run(command, **kwargs)
            if 'SHOW GRANTS' in command[-1]:
                return subprocess.CompletedProcess(command, 0, 'GRANT ALL PRIVILEGES ON *.* TO user\n', '')
            return result
        with patch('aurora.database.cli_mysql_staging_restore._run', side_effect=broad):
            with self.assertRaisesRegex(ValueError, 'grants'):
                staging_restore(str(self.config), 'default', str(self.dump), 'aurora_recovery',
                                'aurora_restore_stage_round3', 'aurora_stage', check_only=True)

    def test_rejects_grant_option(self):
        self._assert_bad_grant(
            'GRANT SELECT, INSERT, CREATE, DROP, ALTER ON `aurora_restore_stage_round3`.* '
            'TO `aurora_stage`@`localhost` WITH GRANT OPTION\n'
        )

    def test_rejects_grant_for_other_account(self):
        self._assert_bad_grant(
            'GRANT SELECT, INSERT, CREATE, DROP, ALTER ON `aurora_restore_stage_round3`.* '
            'TO `another_user`@`localhost`\n'
        )

    def test_rejects_extra_global_privileges(self):
        self._assert_bad_grant(
            'GRANT SELECT, INSERT, CREATE, DROP, ALTER ON *.* '
            'TO `aurora_stage`@`localhost`\n'
        )

    def test_rejects_grant_to_other_schema(self):
        self._assert_bad_grant(
            'GRANT SELECT, INSERT, CREATE, DROP, ALTER ON `aurora_recovery_test`.* '
            'TO `aurora_stage`@`localhost`\n'
        )

    def _assert_bad_grant(self, grant):
        def modified(command, **kwargs):
            if 'SHOW GRANTS' in command[-1]:
                return subprocess.CompletedProcess(command, 0, grant, '')
            return self.fake_run(command, **kwargs)
        with patch('aurora.database.cli_mysql_staging_restore._run', side_effect=modified):
            with self.assertRaisesRegex(ValueError, 'grants'):
                staging_restore(str(self.config), 'default', str(self.dump),
                                'aurora_recovery', 'aurora_restore_stage_round3',
                                'aurora_stage', check_only=True)

    def test_cancel_without_import(self):
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            self.run_stage(check_only=False, confirm=lambda _: False)

    def test_import_success(self):
        with patch('aurora.database.cli_mysql_staging_restore.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, b'', b'')) as importer:
            self.run_stage(check_only=False, confirm=lambda _: True)
        self.assertEqual(importer.call_count, 1)
        self.assertEqual(importer.call_args.args[0][0], 'mysql')

    def test_import_failure_reports_partial(self):
        with patch('aurora.database.cli_mysql_staging_restore.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 1, b'', b'error')):
            with self.assertRaisesRegex(ValueError, 'partial'):
                self.run_stage(check_only=False, confirm=lambda _: True)


if __name__ == '__main__':
    unittest.main()
