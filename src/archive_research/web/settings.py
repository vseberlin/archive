import os
from pathlib import Path
import tomllib

from archive_research.common import require, work_root, plain_file

from archive_research.config import Configuration

CONFIG = Configuration.load()
PROJECT_FILE, MACHINE_FILE = CONFIG.project_file, CONFIG.machine_file
PROJECT, MACHINE, PILOT_ROOT = CONFIG.project, CONFIG.machine, CONFIG.runtime_root
SECRET_KEY = MACHINE['secret_key']
DEBUG = False
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '[::1]']
ROOT_URLCONF = 'archive_research.web.urls'
INSTALLED_APPS = ['django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
                  'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
                  'archive_research.web.apps.ResearchWebConfig']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware',
              'django.contrib.sessions.middleware.SessionMiddleware',
              'django.middleware.common.CommonMiddleware',
              'django.middleware.csrf.CsrfViewMiddleware',
              'django.contrib.auth.middleware.AuthenticationMiddleware',
              'django.contrib.messages.middleware.MessageMiddleware',
              'django.middleware.clickjacking.XFrameOptionsMiddleware',
              'archive_research.web.views.PrivateResponseMiddleware']
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
