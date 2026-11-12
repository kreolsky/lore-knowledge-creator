# Profile settings

Opened from the header. Sections: Profile, MCP, Private keys, Colors, Sound; the section you used last opens again.

## Profile

Name, email, password and the Quick Lock PIN for the [screen and page lock](help:pin-lock). The Lore version is shown here too.

## MCP

MCP servers that the administrator or your group's moderator connected for the chat agent (see [Chat and the agent](help:chat-agent)). Each server says what it needs:

- **your own key** — paste the key in the field. It is stored encrypted, never shown again, and sent only to the server's address. Lore checks the connection at once: "Connected, tools: N", or why it failed (key rejected, server unreachable, not an MCP server) together with what the server itself answered, so you can tell a wrong key from a server that refuses Lore before reading it. "get the key" leads to where the service issues keys;
- **a shared key** — set by the administrator; until it is set, the server is not connected;
- **no key** — connected as is.

The switch on a server turns it off for you only. If the server's address changes, you are asked to enter the key again.

## Private keys

Your API keys for other services, which the agent can use on your behalf — "check the billing with my key". The value is stored encrypted and never shown again; the agent sees only the key's name, description and hosts.

A key has:

- **name** and **description** — the description tells the agent what the key is for;
- **value** — leave the field empty when editing to keep the stored key;
- **hosts** — where the key may be sent: the server name from the API address (for `https://api.openai.com/v1/…` it is `api.openai.com`), with a port only if it is not 443. `*.example.com` covers all subdomains; list `example.com` itself separately;
- **how it is passed** — Bearer, X-API-Key, Basic (enter `login:password`), in the URL as a query parameter, or custom: your own header, with `{{value}}` replaced by the key and `{{value_b64}}` by its base64;
- **Available to the agent** — the switch that lets the agent use the key.

What the agent can do with a key: send HTTPS requests to the key's hosts only. Reading requests (GET, HEAD) run at once; any request that sends data asks your approval every time, even with "Auto" checked. Redirects are not followed, and the key's value is removed from everything the agent reads back. External agents connected through an API key (see [External agent (MCP)](help:external-agent)) never get your keys.

If a key no longer decrypts (for example, after a restore from backup), it is marked "Re-enter key".

## Colors

Recolors the theme that is on now: background, text, accent, status and highlight colors. Each theme keeps its own colors; every color resets on its own, "Reset all" returns the whole theme to its defaults. A color with low contrast against its background shows a warning. "Export" saves both themes' colors as a JSON file, "Import" restores them from such a file. Your colors are kept in your account and are not applied on public pages.

## Sound

The sound played when the agent finishes a turn or asks you to confirm a change: one of four built-in sounds (Chime, Pop, Bell, Soft), each with a "Play" button, or "My sound" — your own audio file up to 500 KB; short sounds work best. Your choice follows you to every device. The sound itself is turned on per project in the "Settings" tab, "Interface" section (see [Chat and the agent](help:chat-agent)).
