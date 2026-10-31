# The staff console: built once, configured at start-up, deployed separately from the API.
FROM node:22.11.0-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run typecheck && npm test && npx vite build

FROM nginx:1.27.3-alpine
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY nginx/40-console-config.sh /docker-entrypoint.d/40-console-config.sh
RUN chmod 755 /docker-entrypoint.d/40-console-config.sh
COPY --from=build /app/dist /usr/share/nginx/html
# Cloud Run injects PORT; compose sets API_UPSTREAM to the API container.
ENV PORT=8080 API_UPSTREAM=http://api:8080 CONSOLE_MODE=auto CONSOLE_API_BASE=
EXPOSE 8080
