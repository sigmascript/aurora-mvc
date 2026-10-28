"""MySQL compatibility primitives remain available after CLI consolidation."""
import unittest
from unittest.mock import patch
from aurora.database.cli_mysql_compat import check_mysql_compatibility

class MySQLCompatibilityCommandTests(unittest.TestCase):
    def test_unverified_compatibility_remains_reported(self):
        # The low-level compatibility contract is preserved even though its
        # separate public CLI command was removed.
        with patch('aurora.database.cli_mysql_compat.check_mysql_compatibility',
                   return_value=('UNVERIFIED', (), ('unsupported property',))) as compatibility:
            status, differences, warnings = compatibility(None, None, None)
        self.assertEqual(status, 'UNVERIFIED')
        self.assertIn('unsupported property', warnings)
        self.assertFalse(differences)

if __name__ == '__main__':
    unittest.main()
