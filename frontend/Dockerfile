# ---- build: bundle the React app ------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ---- runtime: serve dist/ and proxy /api to the ZBS API --------------------
# Runs as uid 101 (non-root), listens on 8080 by default.
FROM nginxinc/nginx-unprivileged:1.27-alpine

RUN rm /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/zbs.conf.template

EXPOSE 8080
