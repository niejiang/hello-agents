"""Look up an attraction's own POI photo through Amap."""
import logging
from urllib.parse import urlsplit

import requests
from ..config import get_settings

logger = logging.getLogger(__name__)


def find_poi_photo(name: str, city: str | None) -> str | None:
    if not city or not city.strip() or not name or not name.strip():
        return None
    key = get_settings().amap_api_key
    if not key:
        return None
    try:
        response = requests.get(
            'https://restapi.amap.com/v5/place/text',
            params={'key': key, 'keywords': name, 'region': city,
                    'city_limit': 'true', 'show_fields': 'photos', 'page_size': 10},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if data.get('status') != '1':
            logger.warning('高德 POI 图片查询失败，错误码: %s', data.get('infocode'))
            return None
        expected = ''.join(name.split()).casefold()
        for poi in data.get('pois') or []:
            if not isinstance(poi, dict) or ''.join(str(poi.get('name', '')).split()).casefold() != expected:
                continue
            photos = poi.get('photos') or []
            if not isinstance(photos, list):
                continue
            for photo in photos:
                url = photo.get('url') if isinstance(photo, dict) else None
                if isinstance(url, str) and urlsplit(url).scheme in ('http', 'https') and urlsplit(url).hostname:
                    return url
    except (requests.RequestException, ValueError, TypeError, TimeoutError):
        logger.warning('高德 POI 图片查询失败', exc_info=False)
    return None
