import base64
import json
import os
import re
import secrets as pysecrets
import shutil
import struct
import uuid as uuidlib
import zipfile

from modules.database import execute_query

DATA_DIR = os.environ.get('CLIENTGEN_DIR', '/data/clientgen')

# Настройки встроенного генератора (GitHub Actions, порт rdgen)
GH_USER = os.environ.get('GH_USER', '')            # владелец репозитория с воркфлоу
GH_REPO = os.environ.get('GH_REPO', 'rustdesk-api')  # репозиторий с воркфлоу generator-*.yml
GH_BRANCH = os.environ.get('GH_BRANCH', 'main')    # ветка для workflow_dispatch
GH_TOKEN = os.environ.get('GH_TOKEN', '')          # PAT с правом actions:write
GENURL = os.environ.get('GENURL', '')              # публичный URL этого сервера (для раннеров GitHub)
ZIP_PASSWORD = os.environ.get('ZIP_PASSWORD', '')  # пароль AES-архива с секретами сборки
PROTOCOL = os.environ.get('PROTOCOL', 'https')
SH_SECRET = os.environ.get('SH_SECRET', '')        # секрет self-hosted раннера (sh-generator)

# правила валидации совпадают с формой rdgen (PLATFORM/VERSION/... choices)
PLATFORM_CHOICES = ('windows', 'windows-x86', 'linux', 'android', 'macos')
VERSION_CHOICES = ('master', '1.4.9', '1.4.8', '1.4.7', '1.4.6', '1.4.5',
                   '1.4.4', '1.4.3', '1.4.2', '1.4.1', '1.4.0')
DIRECTION_CHOICES = ('incoming', 'outgoing', 'both')
INSTALLATION_CHOICES = ('installationY', 'installationN')
SETTINGS_CHOICES = ('settingsY', 'settingsN')

WORKFLOW_BY_PLATFORM = {
    'windows': 'generator-windows.yml',
    'windows-x86': 'generator-windows-x86.yml',
    'linux': 'generator-linux.yml',
    'android': 'generator-android.yml',
    'macos': 'generator-macos.yml',
}

DEFAULT_CONFIG = {
    "platform": "windows", "version": "1.4.9",
    "appname": "", "direction": "both",
    "installation": "installationY", "settings": "settingsY",
    "serverIP": "", "key": "", "apiServer": "",
    "iconbase64": "", "logobase64": "",
    "permanentPassword": "", "defaultManual": "", "overrideManual": "", "note": "",
}


def generator_configured():
    """Достаточно ли настроек для запуска сборки через GitHub Actions."""
    return bool(GH_USER and GH_TOKEN and GENURL and ZIP_PASSWORD)


def _png_dims(data_url):
    if not isinstance(data_url, str) or ';base64,' not in data_url:
        return None
    header, encoded = data_url.split(';base64,', 1)
    if 'image/png' not in header:
        return None
    try:
        data = base64.b64decode(encoded)
    except Exception:
        return None
    if len(data) < 24 or data[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    return struct.unpack('>II', data[16:24])


def validate_config(name, cfg):
    errors = {}
    if not (name or '').strip():
        errors['name'] = 'This field is required.'
    elif not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        errors['name'] = 'English letters, digits, "-" and "_" only (no spaces).'
    if not isinstance(cfg, dict):
        return {'config_json': 'Must be an object.'}
    appname = (cfg.get('appname') or '').strip()
    if not appname:
        errors['appname'] = 'This field is required.'
    elif not appname.isascii():
        errors['appname'] = 'English characters only.'
    for field, choices in (('platform', PLATFORM_CHOICES), ('version', VERSION_CHOICES),
                           ('direction', DIRECTION_CHOICES), ('installation', INSTALLATION_CHOICES),
                           ('settings', SETTINGS_CHOICES)):
        if cfg.get(field) not in choices:
            errors[field] = 'Invalid choice. Must be one of: %s' % list(choices)
    for field in ('serverIP', 'key', 'apiServer', 'permanentPassword',
                  'defaultManual', 'overrideManual', 'note'):
        if cfg.get(field) is not None and not isinstance(cfg.get(field), str):
            errors[field] = 'Must be a string.'
    if cfg.get('iconbase64'):
        dims = _png_dims(cfg['iconbase64'])
        if dims is None:
            errors['iconbase64'] = 'Only PNG images are allowed.'
        elif dims[0] != dims[1]:
            errors['iconbase64'] = 'Icon dimensions must be square.'
    if cfg.get('logobase64') and _png_dims(cfg['logobase64']) is None:
        errors['logobase64'] = 'Only PNG images are allowed.'
    return errors


def _row(r):
    if not r:
        return None
    return dict(r)


def list_configs():
    return execute_query(
        "SELECT * FROM client_configs ORDER BY id", fetch_all=True) or []


def get_config(cid):
    return _row(execute_query(
        "SELECT * FROM client_configs WHERE id=%s", (cid,), fetch_one=True))


def get_config_by_uuid(uuid_val):
    return _row(execute_query(
        "SELECT * FROM client_configs WHERE uuid=%s", (uuid_val,), fetch_one=True))


def create_config(name, platform, version, author, config_json):
    return execute_query("""
        INSERT INTO client_configs (name, platform, version, author, config_json)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (name, platform, version, author, config_json), fetch_one=True)['id']


def update_config(cid, name, platform, version, config_json):
    # при изменении конфигурации сбрасываем сборку и удаляем артефакт
    cfg = get_config(cid)
    if cfg and cfg['artifact_dir'] and os.path.isdir(cfg['artifact_dir']):
        shutil.rmtree(cfg['artifact_dir'], ignore_errors=True)
    execute_query("""
        UPDATE client_configs SET name=%s, platform=%s, version=%s, config_json=%s,
            build_status='none', build_log='', build_date=NULL, artifact_dir='',
            uuid='', github_run_id='', build_token='', updated_at=NOW()
        WHERE id=%s
    """, (name, platform, version, config_json, cid))


def delete_config(cid):
    cfg = get_config(cid)
    if cfg and cfg['artifact_dir'] and os.path.isdir(cfg['artifact_dir']):
        shutil.rmtree(cfg['artifact_dir'], ignore_errors=True)
    execute_query("DELETE FROM client_configs WHERE id=%s", (cid,))


def duplicate_config(cid, author):
    cfg = get_config(cid)
    if not cfg:
        return None
    return create_config(cfg['name'] + ' (copy)', cfg['platform'], cfg['version'],
                         author, cfg['config_json'])


def _artifact_dir(cid):
    d = os.path.join(DATA_DIR, str(cid))
    os.makedirs(d, exist_ok=True)
    return d


def _png_dir(uuid_val):
    d = os.path.join(DATA_DIR, 'png', uuid_val)
    os.makedirs(d, exist_ok=True)
    return d


def _zip_dir():
    d = os.path.join(DATA_DIR, 'temp_zips')
    os.makedirs(d, exist_ok=True)
    return d


def _save_data_url_png(data_url, path):
    """Сохраняет PNG из data-URL (base64). Возвращает True при успехе."""
    if not isinstance(data_url, str) or ';base64,' not in data_url:
        return False
    try:
        _, encoded = data_url.split(';base64,', 1)
        with open(path, 'wb') as f:
            f.write(base64.b64decode(encoded))
        return True
    except Exception:
        return False


def _parse_manual(text):
    """Строки key=value -> dict (как default/override manual в rdgen)."""
    out = {}
    for line in (text or '').splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out


def build_custom_json(cfg):
    """custom.txt (JSON) кастомного клиента — портировано из rdgen."""
    decoded = {}
    direction = cfg.get('direction', 'both')
    if direction != 'both':
        decoded['conn-type'] = direction
    if cfg.get('installation') == 'installationN':
        decoded['disable-installation'] = 'Y'
    if cfg.get('settings') == 'settingsN':
        decoded['disable-settings'] = 'Y'
    appname = (cfg.get('appname') or '').strip()
    if appname and appname.lower() != 'rustdesk':
        decoded['app-name'] = appname
    if cfg.get('permanentPassword'):
        decoded['password'] = cfg['permanentPassword']
    decoded['default-settings'] = _parse_manual(cfg.get('defaultManual'))
    decoded['override-settings'] = _parse_manual(cfg.get('overrideManual'))
    return json.dumps(decoded)


def _github_api(path):
    return 'https://api.github.com/repos/%s/%s/%s' % (GH_USER, GH_REPO, path)


def _github_headers():
    return {
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + GH_TOKEN,
        'X-GitHub-Api-Version': '2026-03-10',
    }


def start_build(cid):
    """Запускает сборку кастомного клиента через GitHub Actions (без rdgen-cli)."""
    cfg = get_config(cid)
    if not cfg:
        return False, 'not found'
    if cfg['build_status'] == 'running':
        return False, 'already running'
    if not generator_configured():
        return False, 'Генератор не настроен (GH_USER/GH_TOKEN/GENURL/ZIP_PASSWORD)'

    try:
        data = json.loads(cfg['config_json'] or '{}')
    except ValueError:
        return False, 'invalid config_json'

    import requests
    try:
        import pyzipper
    except ImportError:
        return False, 'pyzipper не установлен'

    # перед новой сборкой удаляем старые дистрибутивы и архив
    if cfg['artifact_dir'] and os.path.isdir(cfg['artifact_dir']):
        shutil.rmtree(cfg['artifact_dir'], ignore_errors=True)

    platform = cfg.get('platform') or 'windows'
    version = cfg.get('version') or '1.4.9'
    filename = cfg['name']
    myuuid = str(uuidlib.uuid4())
    build_token = pysecrets.token_hex(16)

    # иконка/логотип: сохраняем PNG, раннер скачает их по GET /get_png
    iconlink_url = iconlink_uuid = iconlink_file = 'false'
    if data.get('iconbase64'):
        p = os.path.join(_png_dir(myuuid), 'icon.png')
        if _save_data_url_png(data['iconbase64'], p):
            iconlink_url, iconlink_uuid, iconlink_file = GENURL, myuuid, 'icon.png'
    logolink_url = logolink_uuid = logolink_file = 'false'
    if data.get('logobase64'):
        p = os.path.join(_png_dir(myuuid), 'logo.png')
        if _save_data_url_png(data['logobase64'], p):
            logolink_url, logolink_uuid, logolink_file = GENURL, myuuid, 'logo.png'

    custom_b64 = base64.b64encode(build_custom_json(data).encode('ascii')).decode('ascii')
    server = data.get('serverIP') or 'rs-ny.rustdesk.com'
    key = data.get('key') or 'OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw='
    api_server = data.get('apiServer') or (server + ':21114')

    inputs_raw = {
        'server': server,
        'key': key,
        'apiServer': api_server,
        'custom': custom_b64,
        'uuid': myuuid,
        'token': build_token,
        'iconlink_url': iconlink_url,
        'iconlink_uuid': iconlink_uuid,
        'iconlink_file': iconlink_file,
        'logolink_url': logolink_url,
        'logolink_uuid': logolink_uuid,
        'logolink_file': logolink_file,
        'privacylink_url': 'false',
        'privacylink_uuid': 'false',
        'privacylink_file': 'false',
        'appname': (data.get('appname') or 'rustdesk').strip(),
        'genurl': GENURL,
        'urlLink': 'https://rustdesk.com',
        'downloadLink': 'https://rustdesk.com/download',
        'delayFix': 'true',
        'rdgen': 'true',
        'xOffline': 'false',
        'removeNewVersionNotif': 'false',
        'compname': 'Purslane Ltd',
        'androidappid': 'com.carriez.flutter_hbb',
        'filename': filename,
    }

    # шифруем входы сборки в AES-zip; раннер скачает его по GET /get_zip.
    # uuid сборки в имени нужен для удаления файла по POST /cleanzip
    zip_filename = 'secrets_%s_%s.zip' % (myuuid, uuidlib.uuid4())
    zip_path = os.path.join(_zip_dir(), zip_filename)
    tmp_json = zip_path + '.json'
    try:
        with open(tmp_json, 'w', encoding='utf-8') as f:
            json.dump(inputs_raw, f)
        with pyzipper.AESZipFile(zip_path, 'w', compression=pyzipper.ZIP_LZMA,
                                 encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(ZIP_PASSWORD.encode())
            zf.write(tmp_json, arcname='secrets.json')
    finally:
        if os.path.exists(tmp_json):
            os.remove(tmp_json)

    workflow = WORKFLOW_BY_PLATFORM.get(platform, 'generator-windows.yml')
    if platform == 'windows' and SH_SECRET and data.get('sh_secret_field') == SH_SECRET:
        workflow = 'sh-generator-windows.yml'

    dispatch_url = _github_api('actions/workflows/%s/dispatches' % workflow)
    payload = {
        'ref': GH_BRANCH,
        'inputs': {
            'version': version,
            'zip_url': json.dumps({'url': GENURL, 'file': zip_filename}),
        },
        'return_run_details': True,
    }
    try:
        r = requests.post(dispatch_url, json=payload, headers=_github_headers(), timeout=30)
    except Exception as e:
        return False, 'GitHub API: %s' % e
    if r.status_code not in (200, 201, 204):
        details = ''
        try:
            details = r.json().get('message', '')
        except Exception:
            pass
        return False, 'GitHub отклонил запуск сборки (HTTP %s)%s' % (
            r.status_code, ': ' + details if details else '')

    # ID запуска: return_run_details может вернуть его в разных полях,
    # а при 204 тела нет вовсе — тогда ID будет определён позже по списку запусков
    run_id = ''
    log_url = ''
    try:
        body = r.json()
        run_id = str(body.get('workflow_run_id') or body.get('id') or '')
        log_url = body.get('html_url') or body.get('workflow_run_url') or ''
    except Exception:
        pass

    execute_query("""
        UPDATE client_configs SET build_status='running', build_log=%s, build_date=NULL,
            artifact_dir='', uuid=%s, github_run_id=%s, build_token=%s, updated_at=NOW()
        WHERE id=%s
    """, (log_url, myuuid, run_id, build_token, cid))
    return True, 'started'


def _github_run_status(run_id):
    """Состояние запуска GitHub Actions: (status, conclusion, html_url)."""
    if not GH_TOKEN:
        return None, None, ''
    import requests
    try:
        r = requests.get(_github_api('actions/runs/%s' % run_id),
                         headers=_github_headers(), timeout=30)
        if r.status_code != 200:
            return None, None, ''
        d = r.json()
        return d.get('status'), d.get('conclusion'), d.get('html_url') or ''
    except Exception:
        return None, None, ''


def _resolve_run_id(workflow, branch):
    """ID последнего запуска воркфлоу на ветке (когда dispatch не вернул ID)."""
    if not GH_TOKEN:
        return ''
    import requests
    try:
        r = requests.get(
            _github_api('actions/workflows/%s/runs' % workflow),
            params={'branch': branch, 'event': 'workflow_dispatch', 'per_page': 5},
            headers=_github_headers(), timeout=30)
        if r.status_code != 200:
            return ''
        runs = r.json().get('workflow_runs') or []
        if runs:
            return str(runs[0].get('id') or '')
    except Exception:
        pass
    return ''


def refresh_build_status(cid):
    """Ленивый поллинг GitHub (как _get_run_status в rdgen). Вызывается из /status."""
    cfg = get_config(cid)
    if not cfg or cfg['build_status'] != 'running':
        return cfg
    run_id = (cfg.get('github_run_id') or '').strip()
    if not run_id:
        # ID запуска не был получен при dispatch — определяем по списку запусков
        workflow = WORKFLOW_BY_PLATFORM.get(cfg.get('platform') or 'windows',
                                            'generator-windows.yml')
        run_id = _resolve_run_id(workflow, GH_BRANCH)
        if not run_id:
            return cfg
        execute_query(
            "UPDATE client_configs SET github_run_id=%s, updated_at=NOW() WHERE id=%s",
            (run_id, cid))
    status, conclusion, html_url = _github_run_status(run_id)
    if status == 'completed' and conclusion:
        if conclusion == 'success':
            execute_query("""
                UPDATE client_configs SET build_status='success', build_date=NOW(),
                    artifact_dir=%s, updated_at=NOW() WHERE id=%s
            """, (_artifact_dir(cid), cid))
        else:
            execute_query("""
                UPDATE client_configs SET build_status='failed',
                    build_log=COALESCE(NULLIF(build_log,''), '') || %s, updated_at=NOW()
                WHERE id=%s
            """, ('\nGitHub run: ' + html_url if html_url else '', cid))
        return get_config(cid)
    return cfg


def refresh_running_configs():
    """Обновить статусы всех выполняющихся сборок (вызывается при показе списка)."""
    for cfg in list_configs():
        if cfg['build_status'] == 'running':
            refresh_build_status(cfg['id'])


def apply_callback_status(uuid_val, status):
    """Обработка статуса сборки из воркфлоу (POST /updategh)."""
    cfg = get_config_by_uuid(uuid_val)
    if not cfg:
        return False
    status = (status or '').strip()
    if status in ('success',):
        execute_query("""
            UPDATE client_configs SET build_status='success', build_date=NOW(),
                artifact_dir=%s, updated_at=NOW() WHERE id=%s
        """, (_artifact_dir(cfg['id']), cfg['id']))
    elif status in ('failure', 'cancelled', 'timed_out', 'skipped'):
        execute_query(
            "UPDATE client_configs SET build_status='failed', updated_at=NOW() WHERE id=%s",
            (cfg['id'],))
    else:
        execute_query(
            "UPDATE client_configs SET build_status='running', updated_at=NOW() WHERE id=%s",
            (cfg['id'],))
    return True


def save_artifact(uuid_val, filename, stream):
    """Сохраняет артефакт сборки из воркфлоу (POST /save_custom_client)."""
    cfg = get_config_by_uuid(uuid_val)
    if not cfg:
        return None
    safe = os.path.basename(filename or '')
    if not safe:
        return None
    art = _artifact_dir(cfg['id'])
    path = os.path.join(art, safe)
    stream.save(path)
    return path


def temp_zip_path(filename):
    """Путь к zip с секретами сборки (с защитой от выхода за каталог)."""
    base = os.path.abspath(_zip_dir())
    path = os.path.abspath(os.path.join(base, filename or ''))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        return None
    return path


def cleanup_temp_zips(uuid_val):
    """Удаляет zip с секретами по uuid сборки (POST /cleanzip)."""
    if not uuid_val or not re.fullmatch(r'[0-9a-f-]{36}', uuid_val):
        return 0
    removed = 0
    d = _zip_dir()
    for f in os.listdir(d):
        if f.endswith('.zip') and uuid_val in f:
            try:
                os.remove(os.path.join(d, f))
                removed += 1
            except OSError:
                pass
    return removed


ARTIFACT_ZIP_NAME = 'build.zip'


def artifact_zip(cid):
    """Создаёт zip из артефактов и возвращает путь (или None).

    Если архив уже был подготовлен ранее — возвращается готовый файл
    без повторной генерации (иначе build.zip запаковался бы сам в себя).
    """
    cfg = get_config(cid)
    if not cfg or cfg['build_status'] != 'success':
        return None
    art = cfg['artifact_dir'] or _artifact_dir(cid)
    if not art or not os.path.isdir(art):
        return None
    zpath = os.path.join(art, ARTIFACT_ZIP_NAME)
    # архив уже собран — отдаём его как есть, не пересоздавая
    if os.path.isfile(zpath):
        return zpath
    files = [f for f in os.listdir(art)
             if os.path.isfile(os.path.join(art, f))
             and f not in ('config.json', ARTIFACT_ZIP_NAME)]
    if not files:
        return None
    if len(files) == 1:
        return os.path.join(art, files[0])
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(os.path.join(art, f), f)
    return zpath
