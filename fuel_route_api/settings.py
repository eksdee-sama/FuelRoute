import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-change-me-in-production-abc123xyz')
DEBUG = os.environ.get('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '*').split(',')

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'rest_framework',
    'api',
]

MIDDLEWARE = [
    'django.middleware.gzip.GZipMiddleware',       # compress JSON/HTML responses (3-5x smaller)
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'fuel_route_api.urls'
WSGI_APPLICATION = 'fuel_route_api.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        'CONN_MAX_AGE': None,   # persistent connection — skip reconnect overhead
    }
}

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'TIMEOUT': 3600,
    }
}

REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
    'DEFAULT_PARSER_CLASSES': ['rest_framework.parsers.JSONParser'],
}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_TZ = True
STATIC_URL = '/static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Application settings
CSV_FILE_PATH = BASE_DIR / 'fuel-prices-for-be-assessment.csv'
OSRM_BASE_URL = os.environ.get('OSRM_BASE_URL', 'https://router.project-osrm.org')
NOMINATIM_BASE_URL = os.environ.get('NOMINATIM_BASE_URL', 'https://nominatim.openstreetmap.org')
NOMINATIM_USER_AGENT = os.environ.get('NOMINATIM_USER_AGENT', 'fuel-route-api/1.0')
# Optional: get a free key at https://openrouteservice.org — much faster than public OSRM
ORS_API_KEY = os.environ.get('ORS_API_KEY', '')

VEHICLE_TANK_RANGE_MILES = 500
VEHICLE_MPG = 10

# Max perpendicular distance (miles) from the route polyline to include a station
STATION_ROUTE_PROXIMITY_MILES = float(os.environ.get('STATION_ROUTE_PROXIMITY_MILES', '5.0'))

ROUTE_CACHE_TIMEOUT = 3600  # seconds
