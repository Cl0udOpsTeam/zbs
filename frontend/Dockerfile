# ---- build: bundle the React app ------------------------------------------
FROM node:22-alpine@sha256:76789712cd1ae89a1225eac9077010d68987a423588042dac30446f502f1858c AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ---- runtime: serve dist/ and proxy /api to the ZBS API --------------------
# Runs as uid 101 (non-root), listens on 8080 by default.
# Digest-pinned (multi-arch index) - refresh deliberately, not incidentally.
FROM nginxinc/nginx-unprivileged:1.27-alpine@sha256:28d91bdce70ad09025ea901458fdd149259d8e05982ade79d4ef2c0d9470eb48

RUN rm /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/zbs.conf.template

EXPOSE 8080
