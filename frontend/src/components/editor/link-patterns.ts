/** Regex factories for markdown link patterns. Each call returns a fresh instance (safe for .exec loops). */
// SYSTEM: link-patterns — regex factories for 5 link types (note, ref, doc, ext, bare URL)

export const noteLink = () => /\[{1,2}([^\]]+)\]{1,2}\((note:[^)]+)\)\]?/g;
export const refLink = () => /!?\[{1,2}([^\]]+)\]{1,2}\((ref:[^)]+)\)\]?/g;
export const docLink = () => /\[{1,2}([^\]]+)\]{1,2}\((?!http|#|mailto:|note:|ref:)(?:doc:)?([^)]+)\)\]?/g;
export const extLink = () => /\[{1,2}([^\]]+)\]{1,2}\((http[^)]+)\)\]?/g;
export const bareUrl = () => /(?<!\]\()https?:\/\/[^\s)>\]]+/g;
