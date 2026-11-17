FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
# WHY: `npm ci` from the committed lock — CI, gray and prod install the same tree.
# Refreshing the lock is a deliberate act, done ONLY inside Linux (a Mac-made lock
# lacks the linux-x64 musl/gnu native bindings — rolldown, oxide, lightningcss):
#   docker run --rm -v "$PWD/frontend:/app" -w /app node:22-alpine sh -c \
#     'rm -rf node_modules package-lock.json && npm install --legacy-peer-deps --package-lock-only'
# (or `npm update --legacy-peer-deps --package-lock-only`), committed with the package.json edit.
RUN npm ci --legacy-peer-deps
COPY . .
CMD ["npm", "run", "dev"]
