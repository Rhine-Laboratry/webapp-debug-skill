from __future__ import annotations

from pathlib import Path

from webapp_debug_skill.php_static import extract_php_classes, safe_read_text, string_literals


def test_extract_php_classes_methods_and_line_numbers() -> None:
    text = """<?php
namespace App\\Controller;

class UsersController extends AppController
{
    public function index()
    {
        $this->set('users', []);
    }

    protected function helper()
    {
    }
}
"""

    classes = extract_php_classes(text)

    assert classes[0].name == "UsersController"
    assert classes[0].namespace == "App\\Controller"
    assert classes[0].line == 4
    assert [(method.visibility, method.name, method.line) for method in classes[0].methods] == [
        ("public", "index", 6),
        ("protected", "helper", 11),
    ]
    assert "users" in string_literals(classes[0].methods[0].body)


def test_safe_read_text_rejects_binary_invalid_encoding_and_redacts_secret(tmp_path: Path) -> None:
    binary = tmp_path / "binary.php"
    invalid = tmp_path / "invalid.php"
    secret = tmp_path / "secret.php"
    binary.write_bytes(b"a\x00b")
    invalid.write_bytes(b"\xff")
    secret.write_text("<?php echo 'SECRET_MARKER_STATIC';", encoding="utf-8")

    assert safe_read_text(binary).warning == "BINARY_FILE"
    assert safe_read_text(invalid).warning == "INVALID_ENCODING"
    secret_result = safe_read_text(secret)
    assert secret_result.warning == "SECRET_REDACTED"
    assert "SECRET_MARKER_STATIC" not in (secret_result.text or "")
