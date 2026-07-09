#!/bin/sh
# ============================================================================
# Entrypoint da imagem `frontend`  — nginx servindo o SPA estático
# (Vite dist) e fazendo reverse-proxy de /api → serviço da API (uvicorn).
#
# Substitui a parte de nginx do antigo start.sh. NÃO sobe uvicorn nem gera
# APP_MASTER_KEY — esta imagem não tem nada de Python. O upstream da API é
# parametrizado por ${API_UPSTREAM} (default: centralops:8000, o nome do serviço
# no compose) e injetado por envsubst com lista EXPLÍCITA de variáveis, para não
# expandir as variáveis de runtime do nginx ($host, $remote_addr, ...).
# ============================================================================
set -eu

export NGINX_SERVER_NAME="${NGINX_SERVER_NAME:-_}"
export ENABLE_HTTPS="${ENABLE_HTTPS:-0}"
export API_UPSTREAM="${API_UPSTREAM:-centralops:8000}"
export NGINX_SSL_CERT_PATH="${NGINX_SSL_CERT_PATH:-/certs/tls.crt}"
export NGINX_SSL_KEY_PATH="${NGINX_SSL_KEY_PATH:-/certs/tls.key}"
export NGINX_SSL_CERT_VALID_DAYS="${NGINX_SSL_CERT_VALID_DAYS:-365}"

TEMPLATE_DIR="/etc/nginx/templates"
HTTP_TEMPLATE="${TEMPLATE_DIR}/nginx.http.conf.template"
HTTPS_TEMPLATE="${TEMPLATE_DIR}/nginx.https.conf.template"
DEFAULT_CONF="/etc/nginx/conf.d/default.conf"
AUTO_SSL_DIR="/etc/nginx/ssl"

# Lista EXPLÍCITA das variáveis que o envsubst pode expandir — protege as
# variáveis de runtime do nginx ($host etc.) de serem apagadas.
ENVSUBST_VARS='${NGINX_SERVER_NAME} ${API_UPSTREAM} ${NGINX_SSL_CERT_PATH} ${NGINX_SSL_KEY_PATH}'

generate_self_signed_certificate {
    mkdir -p "${AUTO_SSL_DIR}"
    auto_cert_path="${AUTO_SSL_DIR}/selfsigned.crt"
    auto_key_path="${AUTO_SSL_DIR}/selfsigned.key"
    cert_subject="/CN=localhost"
    san_entries="DNS:localhost,IP:127.0.0.1"

    if [ -f "${auto_cert_path}" ] && [ -f "${auto_key_path}" ]; then
        echo "start-frontend: usando certificado self-signed já gerado."
        export NGINX_SSL_CERT_PATH="${auto_cert_path}"
        export NGINX_SSL_KEY_PATH="${auto_key_path}"
        return
    fi

    first_server_name=$(printf '%s' "${NGINX_SERVER_NAME}" | cut -d' ' -f1 | cut -d',' -f1)
    if [ -n "${first_server_name}" ] && [ "${first_server_name}" != "_" ]; then
        cert_subject="/CN=${first_server_name}"
        case "${first_server_name}" in
            *[!0-9.]*) san_entries="DNS:${first_server_name},${san_entries}" ;;
            *) san_entries="IP:${first_server_name},${san_entries}" ;;
        esac
    fi

    echo "start-frontend: gerando certificado self-signed para HTTPS..."
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout "${auto_key_path}" -out "${auto_cert_path}" \
        -days "${NGINX_SSL_CERT_VALID_DAYS}" -subj "${cert_subject}" \
        -addext "subjectAltName=${san_entries}"
    export NGINX_SSL_CERT_PATH="${auto_cert_path}"
    export NGINX_SSL_KEY_PATH="${auto_key_path}"
}

select_nginx_template {
    normalized=$(printf '%s' "${ENABLE_HTTPS}" | tr '[:upper:]' '[:lower:]')
    if [ "${normalized}" = "1" ] || [ "${normalized}" = "true" ] || [ "${normalized}" = "yes" ]; then
        if [ -f "${NGINX_SSL_CERT_PATH}" ] && [ -f "${NGINX_SSL_KEY_PATH}" ]; then
            echo "start-frontend: usando certificado TLS fornecido."
        elif [ -f "${NGINX_SSL_CERT_PATH}" ] || [ -f "${NGINX_SSL_KEY_PATH}" ]; then
            echo "ERROR: forneça certificado E chave TLS juntos." >&2
            exit 1
        else
            generate_self_signed_certificate
        fi
        envsubst "${ENVSUBST_VARS}" < "${HTTPS_TEMPLATE}" > "${DEFAULT_CONF}"
        return
    fi
    envsubst "${ENVSUBST_VARS}" < "${HTTP_TEMPLATE}" > "${DEFAULT_CONF}"
}

[ -f "${HTTP_TEMPLATE}" ] || { echo "ERROR: ${HTTP_TEMPLATE} não encontrado" >&2; exit 1; }
[ -f "${HTTPS_TEMPLATE}" ] || { echo "ERROR: ${HTTPS_TEMPLATE} não encontrado" >&2; exit 1; }

select_nginx_template
echo "start-frontend: nginx → /api proxy para http://${API_UPSTREAM}"
exec nginx -g 'daemon off;'
