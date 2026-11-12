# Static UI (ui/) served by nginx on port 3000 as container demo3-web.
FROM nginx:1.27-alpine
COPY deploy/ui-nginx.conf /etc/nginx/conf.d/default.conf
COPY ui/ /usr/share/nginx/html/
EXPOSE 3000
