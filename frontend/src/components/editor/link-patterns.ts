/** Regex factories for markdown link patterns. Each call returns a fresh instance (safe for .exec loops). */
// SYSTEM: link-patterns — regex factories for 4 link types (ref, doc, ext, bare URL)

export const refLink = () => /!?\[{1,2}([^\]]+)\]{1,2}\((ref:[^)]+)\)\]?/g;
export const docLink = () => /\[{1,2}([^\]]+)\]{1,2}\((?!http|#|mailto:|ref:)(?:doc:)?([^)]+)\)\]?/g;
export const extLink = () => /\[{1,2}([^\]]+)\]{1,2}\((http[^)]+)\)\]?/g;
export const bareUrl = () => /(?<!\]\()https?:\/\/[^\s)>\]]+/g;
