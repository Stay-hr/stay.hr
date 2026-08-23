FROM node:22-alpine AS deps
WORKDIR /app/web/booking
COPY web/booking/package.json web/booking/package-lock.json ./
RUN npm ci

FROM deps AS dev
ENV NODE_ENV=development
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
EXPOSE 3000
CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0", "--port", "3000"]

FROM node:22-alpine AS builder
WORKDIR /app/web/booking
COPY --from=deps /app/web/booking/node_modules ./node_modules
COPY data/iso3166-countries.json /app/data/iso3166-countries.json
COPY web/booking/ .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs
COPY --from=builder /app/web/booking/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/web/booking/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/web/booking/.next/static ./.next/static
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
