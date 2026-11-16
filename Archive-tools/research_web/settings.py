import os
from pathlib import Path
import tomllib

from archive_common import require, work_root, plain_file

PROJECT_FILE = Path(os.environ['ARCHIVE_RESEARCH_PROJECT'])
MACHINE_FILE = Path(os.environ['ARCHIVE_RESEARCH_MACHINE'])
require(MACHINE_FILE.stat().st_mode & 0o077 == 0, 'Machine configuration must be private')
PROJECT = tomllib.loads(plain_file(PROJECT_FILE).read_text())
MACHINE = tomllib.loads(plain_file(MACHINE_FILE).read_text())
require(PROJECT.get('mode') in ('synthetic', 'local_review'), 'Explicit synthetic or local review mode required')
PILOT_ROOT = work_root(MACHINE['runtime_root'])
require(PROJECT_FILE.absolute().parent == PILOT_ROOT, 'Project must explicitly select its runtime root')
require(PILOT_ROOT.stat().st_mode & 0o077 == 0, 'Runtime directory must be private')
require(PROJECT_FILE.resolve() == PILOT_ROOT / 'project.toml' and
        MACHINE_FILE.resolve() == PILOT_ROOT / 'machine.toml', 'Configuration outside runtime')
for directory in ('records', 'sample', 'cache', 'exports'):
    require((PILOT_ROOT / directory).is_dir() and not (PILOT_ROOT / directory).is_symlink(),
            'Missing or redirected pilot directory')
SECRET_KEY = MACHINE['secret_key']
require(len(SECRET_KEY) >= 50, 'A private random secret is required')
DEBUG = False
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '[::1]']
ROOT_URLCONF = 'research_web.urls'
INSTALLED_APPS = ['django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
                  'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
                  'research_web']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware',
              'django.contrib.sessions.middleware.SessionMiddleware',
              'django.middleware.common.CommonMiddleware',
              'django.middleware.csrf.CsrfViewMiddleware',
              'django.contrib.auth.middleware.AuthenticationMiddleware',
              'django.contrib.messages.middleware.MessageMiddleware',
              'django.middleware.clickjacking.XFrameOptionsMiddleware',
              'research_web.views.PrivateResponseMiddleware']
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'APP_DIRS': True,
              'OPTIONS': {'context_processors': ['django.template.context_processors.request',
                           'django.contrib.auth.context_processors.auth',
                           'django.contrib.messages.context_processors.messages']}}]
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3',
                        'NAME': PILOT_ROOT / 'pilot.sqlite3', 'OPTIONS': {'timeout': 10}}}
# One disposable projection table; built-in auth/session tables use Django migrations.
# Rebuild this table from readable files rather than maintaining schema history.
MIGRATION_MODULES = {'research_web': None}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
TIME_ZONE = 'Europe/Berlin'
LANGUAGE_CODE = 'en'
STATIC_URL = '/static/'
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
SESSION_COOKIE_NAME = 'wf_research_session'
CSRF_COOKIE_NAME = 'wf_research_csrf'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_SAMESITE = 'Strict'
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
AUTH_PASSWORD_VALIDATORS = [{'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'}]
