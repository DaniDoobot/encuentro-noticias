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

    def diagnose_connection(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Runs a series of tests to diagnose WordPress integration issues.
        Returns a diagnostic dictionary.
        """
        url = config.get("WORDPRESS_BASE_URL") or settings.WORDPRESS_BASE_URL or ""
        username = config.get("WORDPRESS_USERNAME") or settings.WORDPRESS_USERNAME or ""
        app_password = settings.WORDPRESS_APPLICATION_PASSWORD or "" # Only read from env/settings
        
        # Clean URL
        url_clean = url.rstrip("/")
        
        # Obscure username
        masked_user = ""
        if username:
            if len(username) <= 2:
                masked_user = username[0] + "*"
            else:
                masked_user = username[:2] + "***"

        warnings = []
        # Path validation
        from urllib.parse import urlparse
        if url:
            parsed = urlparse(url)
            path = parsed.path.lower()
            if path and path not in ("", "/", "/wp-json", "/wp-json/"):
                warnings.append(f"La URL base contiene una ruta de subdirectorio '{parsed.path}'. Lo correcto suele ser configurar solo el dominio raíz (ej. https://miweb.com).")
            
            for keyword in ("/wp-admin", "/mi-cuenta", "/my-account", "/wp-login"):
                if keyword in url.lower():
                    warnings.append(f"La URL base contiene '{keyword}', que es un path de administración o usuario. Debe ser la URL base del sitio.")
        else:
            warnings.append("WORDPRESS_BASE_URL no está configurada.")

        if not username:
            warnings.append("WORDPRESS_USERNAME no está configurado.")
        if not app_password:
            warnings.append("WORDPRESS_APPLICATION_PASSWORD no está configurado en las variables de entorno.")

        checks = []
        checks_dict = {}

        def perform_check(name, check_url, use_auth=False):
            auth = httpx.BasicAuth(username, app_password) if use_auth and username and app_password else None
            check_res = {
                "name": name,
                "url": check_url,
                "status_code": None,
                "content_type": "",
                "ok": False,
                "body_preview": ""
            }
            if not url_clean:
                check_res["body_preview"] = "No se puede realizar: WORDPRESS_BASE_URL vacía."
                return check_res
                
            try:
                with httpx.Client(timeout=10.0) as client:
                    res = client.get(check_url, auth=auth)
                    check_res["status_code"] = res.status_code
                    check_res["content_type"] = res.headers.get("content-type", "")
                    check_res["ok"] = (200 <= res.status_code < 300)
                    
                    text = res.text
                    if len(text) > 200:
                        check_res["body_preview"] = text[:200] + "..."
                    else:
                        check_res["body_preview"] = text
            except Exception as e:
                check_res["body_preview"] = f"Error de conexión: {str(e)}"
            return check_res

        # 1. GET {WORDPRESS_BASE_URL}/wp-json/ (Public REST API)
        c1 = perform_check("public_rest_api", f"{url_clean}/wp-json/", use_auth=False)
        checks.append(c1)
        checks_dict[c1["name"]] = c1

        # 2. GET {WORDPRESS_BASE_URL}/wp-json/wp/v2/posts?per_page=1 (Public posts listing)
        c2 = perform_check("public_posts", f"{url_clean}/wp-json/wp/v2/posts?per_page=1", use_auth=False)
        checks.append(c2)
        checks_dict[c2["name"]] = c2

        # 3. GET {WORDPRESS_BASE_URL}/wp-json/wp/v2/users/me (Authenticated user)
        c3 = perform_check("authenticated_user", f"{url_clean}/wp-json/wp/v2/users/me", use_auth=True)
        checks.append(c3)
        checks_dict[c3["name"]] = c3

        # 4. GET {WORDPRESS_BASE_URL}/wp-json/wp/v2/posts?per_page=1&context=edit (Authenticated posts context=edit)
        c4 = perform_check("authenticated_posts_edit_context", f"{url_clean}/wp-json/wp/v2/posts?per_page=1&context=edit", use_auth=True)
        checks.append(c4)
        checks_dict[c4["name"]] = c4

        # 5. GET {WORDPRESS_BASE_URL}/wp-json/wp/v2/types?context=edit (Authenticated types context=edit)
        c5 = perform_check("authenticated_types_edit_context", f"{url_clean}/wp-json/wp/v2/types?context=edit", use_auth=True)
        checks.append(c5)
        checks_dict[c5["name"]] = c5

        # Deduce status
        users_me_ok = checks_dict.get("authenticated_user", {}).get("ok", False)
        posts_edit_ok = checks_dict.get("authenticated_posts_edit_context", {}).get("ok", False)
        types_edit_ok = checks_dict.get("authenticated_types_edit_context", {}).get("ok", False)

        users_me_blocked = (not users_me_ok) and posts_edit_ok
        authenticated = posts_edit_ok
        can_publish = posts_edit_ok and types_edit_ok

        likely_problem = "none"
        if not url:
            likely_problem = "wrong_base_url"
        else:
            status_codes = [c.get("status_code") for c in checks]
            # If all checks failed to connect or returned 404
            if all(sc is None or sc == 404 for sc in status_codes):
                likely_problem = "wrong_base_url"
            # If public REST API failed
            elif not checks_dict.get("public_rest_api", {}).get("ok", False) and any(sc in (403, 401, 503) for sc in status_codes[:2]):
                likely_problem = "rest_api_blocked_or_waf"
            # If public posts failed
            elif not checks_dict.get("public_posts", {}).get("ok", False):
                likely_problem = "public_posts_blocked"
            # If authenticated posts edit context failed
            elif not authenticated:
                likely_problem = "invalid_credentials"
            # If authenticated works but users/me is blocked
            elif users_me_blocked:
                likely_problem = "users_endpoint_blocked_but_auth_ok"
            # If types edit context failed
            elif not can_publish:
                likely_problem = "insufficient_permissions"

        # If there are subpath warnings, suggest "wrong_base_url"
        if warnings and likely_problem == "none":
            likely_problem = "wrong_base_url"

        return {
            "wordpress_base_url": url,
            "wordpress_username_masked": masked_user,
            "checks": checks,
            "users_me_blocked": users_me_blocked,
            "authenticated": authenticated,
            "can_publish": can_publish,
            "likely_problem": likely_problem,
            "warnings": warnings
        }

    def build_post_payload(self, review: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Builds the REST API payload for a WordPress post from the review data.
        """
        def clean_val(val: Any) -> str:
            if not val:
                return ""
            s = str(val).strip()
            if s.lower() in ("titulo web", "título web", "autor web", "autor web "):
                return ""
            return s

        title_libro = clean_val(review.get("Título del libro"))
        title_web = clean_val(review.get("Título para Web"))
        title_ia = clean_val(review.get("Título del libro detectado por IA"))
        title_art = clean_val(review.get("Título del artículo"))

        author_libro = clean_val(review.get("Autor del libro"))
        author_web = clean_val(review.get("Autor para Web"))
        author_ia = clean_val(review.get("Autor del libro detectado por IA"))

        # wordpress title: priority is title_libro, title_web, title_ia, title_art, "Reseña"
        post_title = (
            title_libro
            or title_web
            or title_ia
            or title_art
            or "Reseña"
        ).strip()

        # used for content layout
        title_book = title_web or title_libro or title_ia or "Sin título"
        author_book = author_web or author_libro or author_ia or "Sin autor"

        summary = review.get("Resumen", "")
        original_url = review.get("URL", "")
        medium = review.get("Medio de publicación", "")
        pub_author = review.get("Autor de la publicación", "")
        pub_date = review.get("Fecha de publicación", "")
        content_type = review.get("Tipo de contenido", "reseña")
        
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

    def publish_draft_post(self, title: str, content: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Publishes a draft post to WordPress using configured endpoints.
        """
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
        payload = {
            "title": title,
            "content": content,
            "status": "draft"
        }
        
        try:
            auth = httpx.BasicAuth(username, app_password)
            with httpx.Client(timeout=20.0) as client:
                res = client.post(f"{url}/wp-json/wp/v2/{post_type}", json=payload, auth=auth)
                if res.status_code in (200, 201):
                    res_data = res.json()
                    return {
                        "success": True,
                        "wordpress_id": res_data.get("id"),
                        "wordpress_url": res_data.get("link"),
                        "status": res_data.get("status")
                    }
                else:
                    return {
                        "success": False,
                        "error": f"Fallo al publicar borrador (HTTP {res.status_code}): {res.text}"
                    }
        except Exception as e:
            return {
                "success": False,
                "error": f"Error de conexión con WordPress: {str(e)}"
            }

wordpress_publisher = WordPressPublisher()
