# Multi-stage: build the static bundle with Node, serve it with nginx —
# the runtime image never needs Node at all (SPEC.md §6 Phase 5 task 5).
FROM node:22-slim AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY --from=build /app/dist /usr/share/nginx/html
# `.template` + this location: the official nginx image's entrypoint
# (`20-envsubst-on-templates.sh`, built in since nginx 1.19) runs
# `envsubst` over every file here at container *start* (not build time)
# and writes the result to /etc/nginx/conf.d/ — see nginx.conf.template's
# own header comment for why PORT/BACKEND_INTERNAL_URL can't just be
# baked in at build time.
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

# Defaults matching docker-compose.yml exactly, so this image's behavior
# is unchanged from before PORT/BACKEND_INTERNAL_URL existed when run via
# `docker run`/compose without overriding them. A platform like Railway
# overrides both at deploy time (README's Railway section) — Railway in
# particular always injects its own PORT per service regardless of this
# default.
ENV PORT=80 \
    BACKEND_INTERNAL_URL=http://backend:8000
EXPOSE 80
