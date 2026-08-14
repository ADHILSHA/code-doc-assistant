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
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
