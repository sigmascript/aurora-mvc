# Application configuration

Aurora projects can use **one small, optional general configuration file** called
`aurora_config.json`. The file is not a database manifest, a Python module, or a
user-preference store. This feature is in the `1.0.0a7` source candidate;
it was not part of the published `1.0.0a6` package. Publication remains approval-gated.

The generated minimal, MVC, and modular starters create the file alongside
`app.py` and load it explicitly. Existing `Aurora(...)` constructors remain
unchanged and do not automatically search the filesystem.

## Example

```json
{
  "format": "aurora.application.v1",
  "application": {
    "name": "Aiomax CMS",
    "timezone": "UTC",
    "locales": ["en", "ku"],
    "default_locale": "en",
    "locale_cookie_name": "site_language"
  },
  "limits": {
    "max_request_body_bytes": 16777216
  },
  "constants": {
    "SITE_TITLE": "Aiomax CMS",
    "FEATURE_ENABLED": true
  }
}
```

From `app.py`:

```python
from pathlib import Path
from aurora import Aurora

ROOT = Path(__file__).resolve().parent
app = Aurora.from_config(ROOT / "aurora_config.json",
                         views_directory=ROOT / "views")
```

`app.settings["name"]` provides the configured name,
`app.settings["timezone"]` the IANA identifier, `app.timezone` the associated
time-zone object, and `app.constants["SITE_TITLE"]` a validated, read-only
constant. Constants are simple JSON scalars only; they are not injected into
Python globals or rendered templates automatically.

Locales and default locale follow Aurora's existing route-locale contract.
Enabling locales changes how URL prefixes are matched; adding locales to an
existing application requires updating its routes appropriately. The locale
cookie must be selected explicitly and remains subject to session-cookie
collision checks.

The optional request-body size limit corresponds to the existing
`max_request_body_bytes` constructor option. **It is not a per-file upload
limit and does not provide secure media-upload processing.**

## Compatibility and validation

- Direct `Aurora(...)` keyword construction is fully supported. No configuration
  file is read unless the developer chooses `Aurora.from_config(...)`.
- Python constructor arguments may provide settings such as `views_directory`,
  session managers, and database lifecycle components. Passing the same option
  both inside JSON and in Python is rejected instead of silently overriding.
- Unknown keys, duplicate JSON keys, malformed identifiers, oversized files,
  nonstandard JSON numeric constants, and unsupported types are rejected.
- All configured settings are loaded and validated once at startup; editing
  `aurora_config.json` later does not silently change a running application.
- The default time zone is `UTC`. Other IANA zones such as `Asia/Baghdad` use
  Python `zoneinfo`; on Windows an IANA database may require installing
  `tzdata` separately. Aurora does **not** modify the operating system clock.
- Configured constants and settings views are read-only. User-editable values
  are application data and should be stored in the database or suitable sessions.

## Three distinct responsibilities

| File | Purpose |
| --- | --- |
| `aurora_config.json` | General, **nonsecret** application defaults |
| `aurora_databases.json` | AuroraSQL connections and optional database aliases |
| `aurora_models.json` | Trusted model registration for database commands |

None of these files belongs under a publicly served `statics/` directory.
Keep database credentials and session secrets in environment variables or an
approved secret manager, and keep user preferences in protected application
data. Do not make JSON files writable by untrusted users. A general application
configuration file must not be treated as a database migration-operation file;
`aurora db migrate -c/--changes` accepts a different, strictly validated format.
