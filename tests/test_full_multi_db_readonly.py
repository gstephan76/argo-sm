"""SELECT-only provisioning does not rewrite already-correct privileges."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / 'scripts/full-multi-db-readonly.py'
spec = importlib.util.spec_from_file_location('db_readonly', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

GRANTS = ["GRANT USAGE ON *.* TO `bookapp`@`%`",
          "GRANT SELECT ON `bookdb`.`books` TO `bookapp`@`%`"]


class ReadOnlyTests(unittest.TestCase):
    def test_correct_grants_are_not_rewritten(self):
        with patch.object(module, 'sql', side_effect=[['%'], GRANTS]) as sql:
            module.apply('db-pod')
            self.assertEqual(sql.call_count, 2)
            self.assertTrue(all('REVOKE' not in c.args[1] for c in sql.call_args_list))

    def test_grant_option_does_not_count_as_readonly(self):
        self.assertFalse(module.select_only([GRANTS[0], GRANTS[1] + ' WITH GRANT OPTION']))

    def test_drift_is_revoked_only_after_direct_select_and_application_check(self):
        calls=[]
        before=[GRANTS[0], "GRANT ALL PRIVILEGES ON `bookdb`.* TO `bookapp`@`%`"]
        after=before+["GRANT SELECT, INSERT ON `bookdb`.`books` TO `bookapp`@`%` WITH GRANT OPTION"]
        def fake_sql(pod, query, root=False):
            calls.append((query,root))
            if query.startswith('SELECT Host'): return ['%']
            if query.startswith('SHOW GRANTS'): return before if len([c for c in calls if c[0].startswith('SHOW GRANTS')])==1 else after
            if query.startswith('SELECT COUNT'): return ['20']
            if 'information_schema.TABLE_PRIVILEGES' in query: return ['INSERT','SELECT']
            return []
        with patch.object(module, 'sql', side_effect=fake_sql):
            module.apply('db-pod')
        statements=[q for q,_ in calls]
        grant=next(i for i,q in enumerate(statements) if q.startswith('GRANT SELECT'))
        check=next(i for i,q in enumerate(statements) if q.startswith('SELECT COUNT'))
        revokes=[(i,q) for i,q in enumerate(statements) if q.startswith('REVOKE')]
        self.assertTrue(revokes)
        self.assertTrue(all(grant < check < i for i,_ in revokes))
        self.assertIn("REVOKE ALL PRIVILEGES ON `bookdb`.* FROM 'bookapp'@'%';",statements)
        self.assertIn("REVOKE INSERT ON `bookdb`.`books` FROM 'bookapp'@'%';",statements)
        self.assertIn("REVOKE GRANT OPTION ON `bookdb`.`books` FROM 'bookapp'@'%';",statements)
        self.assertFalse(any(q.startswith('REVOKE SELECT') for q in statements))

    def test_unknown_role_is_refused_before_mutation(self):
        with patch.object(module, 'sql', side_effect=[['%'], GRANTS+["GRANT `writer` TO `bookapp`@`%`"]]) as sql:
            with self.assertRaisesRegex(module.Refusal, 'Unrecognized account grant'):
                module.apply('db-pod')
            self.assertEqual(sql.call_count,2)

    def test_failed_application_check_never_revokes(self):
        grants=[GRANTS[0], "GRANT ALL PRIVILEGES ON `bookdb`.* TO `bookapp`@`%`"]
        with patch.object(module, 'sql', side_effect=[['%'], grants, [], ['0']]) as sql:
            with self.assertRaisesRegex(module.Refusal, 'SELECT check failed'):
                module.apply('db-pod')
            self.assertTrue(all('REVOKE' not in c.args[1] for c in sql.call_args_list))

    def test_unknown_table_privilege_stops_before_any_revoke(self):
        grants=[GRANTS[0], "GRANT ALL PRIVILEGES ON `bookdb`.* TO `bookapp`@`%`"]
        after=grants+[GRANTS[1]]
        with patch.object(module, 'sql', side_effect=[['%'], grants, [], ['20'], after,
                                                      ['SELECT', 'UNKNOWN FUTURE PRIVILEGE']]) as sql:
            with self.assertRaisesRegex(module.Refusal, 'unrecognized table privilege'):
                module.apply('db-pod')
            self.assertTrue(all('REVOKE' not in c.args[1] for c in sql.call_args_list))

    def test_interrupted_revoke_can_be_retried_with_select_preserved(self):
        grants=[GRANTS[0], "GRANT ALL PRIVILEGES ON `bookdb`.* TO `bookapp`@`%`", GRANTS[1]]
        calls=[]
        fail_once=[True]
        def fake_sql(pod, query, root=False):
            calls.append(query)
            if query.startswith('SELECT Host'): return ['%']
            if query.startswith('SHOW GRANTS'): return grants
            if query.startswith('SELECT COUNT'): return ['20']
            if 'information_schema.TABLE_PRIVILEGES' in query: return ['SELECT']
            if query.startswith('REVOKE') and fail_once[0]:
                fail_once[0]=False
                raise module.Refusal('simulated interruption')
            return []
        with patch.object(module, 'sql', side_effect=fake_sql):
            with self.assertRaisesRegex(module.Refusal, 'simulated interruption'):
                module.apply('db-pod')
            cutoff=len(calls)
            module.apply('db-pod')
        for attempt in (calls[:cutoff], calls[cutoff:]):
            self.assertLess(next(i for i,q in enumerate(attempt) if q.startswith('GRANT SELECT')),
                            next(i for i,q in enumerate(attempt) if q.startswith('REVOKE')))
            self.assertFalse(any(q.startswith('REVOKE SELECT') for q in attempt))


if __name__ == '__main__':
    unittest.main()
