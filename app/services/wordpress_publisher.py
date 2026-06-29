import base64
import httpx
from typing import Dict, Any, Optional
import logging
from app.config import settings

logger = logging.getLogger("encuentro-noticias")

class WordPressPublisher:
    def test_connection(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Tests connection to the WordPress REST API users/me endpoint using Basic Authentication.
        """
        url = config.get("WORDPRESS_BASE_URL") or settings.WORDPRESS_BASE_URL
        username = config.get("WORDPRESS_USERNAME") or settings.WORDPRESS_USERNAME
        app_password = settings.WORDPRESS_APPLICATION_PASSWORD  # Only read from env/settings for security
        
        if not url or not username or not app_password:
            return {
                "success": False,
                "message": "Faltan credenciales de WordPress en variables de entorno o Config (WORDPRESS_BASE_URL, WORDPRESS_USERNAME, WORDPRESS_APPLICATION_PASSWORD)."
            }
            
        url = url.rstrip("/")
        try:
            auth = httpx.BasicAuth(username, app_password)
            with httpx.Client(timeout=15.0) as client:
                res = client.get(f"{url}/wp-json/wp/v2/users/me", auth=auth)
                if res.status_code == 200:
                    user_data = res.json()
                    return {
                        "success": True,
                        "message": f"Conexión exitosa. Usuario: {user_data.get('name')} (ID: {user_data.get('id')})"
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Fallo de autenticación en WordPress (HTTP {res.status_code}): {res.text}"
                    }
        except Exception as e:
            return {
                "success": False,
                "message": f"Error de conexión con WordPress: {str(e)}"
            }

    def build_post_payload(self, review: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Builds the REST API payload for a WordPress post from the review data.
        """
        title_book = review.get("Título del libro", "")
        author_book = review.get("Autor del libro", "")
        summary = review.get("Resumen", "")
        original_url = review.get("URL", "")
        medium = review.get("Medio de publicación", "")
        pub_author = review.get("Autor de la publicación", "")
        pub_date = review.get("Fecha de publicación", "")
        content_type = review.get("Tipo de contenido", "reseña")
        
        # Build HTML content
        post_title = f"Reseña: {title_book} - {author_book}"
        
        post_content = f"""<p><strong>Libro:</strong> {title_book}</p>
<p><strong>Autor del libro:</strong> {author_book}</p>
<p><strong>Medio de origen:</strong> {medium}</p>
{f"<p><strong>Autor de la reseña:</strong> {pub_author}</p>" if pub_author else ""}
{f"<p><strong>Fecha de la publicación original:</strong> {pub_date}</p>" if pub_date else ""}
<p><strong>Tipo de contenido:</strong> {content_type}</p>
<hr />
<p>{summary}</p>
<hr />
<p><em>Reseña original publicada en: <a href=\"{original_url}\" target=\"_blank\">{original_url}</a></em></p>"""

        status = config.get("WORDPRESS_POST_STATUS") or settings.WORDPRESS_POST_STATUS or "draft"
        
        payload = {
            "title": post_title,
            "content": post_content,
            "status": status
        }
        
        cat_id = config.get("WORDPRESS_DEFAULT_CATEGORY_ID") or settings.WORDPRESS_DEFAULT_CATEGORY_ID
        if cat_id:
            try:
                payload["categories"] = [int(cat_id)]
            except ValueError:
                pass
                
        return payload

    def publish_review(self, review: Dict[str, Any], config: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
        """
        Publishes a single review to WordPress (or simulates if dry_run=True).
        """
        if dry_run:
            import random
            sim_id = random.randint(1000, 9999)
            base_url = config.get("WORDPRESS_BASE_URL") or settings.WORDPRESS_BASE_URL or "https://ejemplo.wordpress.com"
            base_url = base_url.rstrip("/")
            return {
                "success": True,
                "wordpress_id": str(sim_id),
                "wordpress_url": f"{base_url}/?p={sim_id}",
                "message": "Publicación simulada en dry_run."
            }
            
        url = config.get("WORDPRESS_BASE_URL") or settings.WORDPRESS_BASE_URL
        username = config.get("WORDPRESS_USERNAME") or settings.WORDPRESS_USERNAME
        app_password = settings.WORDPRESS_APPLICATION_PASSWORD
        post_type = config.get("WORDPRESS_POST_TYPE") or settings.WORDPRESS_POST_TYPE or "posts"
        
        if not url or not username or not app_password:
            return {
                "success": False,
                "error": "Faltan credenciales de WordPress en variables de entorno o Config."
            }
            
        url = url.rstrip("/")
        payload = self.build_post_payload(review, config)
        
        try:
            auth = httpx.BasicAuth(username, app_password)
            with httpx.Client(timeout=20.0) as client:
                res = client.post(f"{url}/wp-json/wp/v2/{post_type}", json=payload, auth=auth)
                if res.status_code in (200, 201):
                    res_data = res.json()
                    wp_id = str(res_data.get("id"))
                    wp_url = res_data.get("link")
                    return {
                        "success": True,
                        "wordpress_id": wp_id,
                        "wordpress_url": wp_url,
                        "message": "Publicación exitosa."
                    }
                else:
                    return {
                        "success": False,
                        "error": f"Fallo al publicar (HTTP {res.status_code}): {res.text}"
                    }
        except Exception as e:
            return {
                "success": False,
                "error": f"Error de conexión con WordPress: {str(e)}"
            }

wordpress_publisher = WordPressPublisher()
