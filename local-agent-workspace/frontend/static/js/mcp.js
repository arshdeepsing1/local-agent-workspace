// The hints an MCP server gives about one of its tools (mcp_client.ANNOTATION_FIELDS: read_only,
// destructive, idempotent, open_world), in words. Display only: they never change an approval.
export function hintText(hints) {
  if (!hints || typeof hints !== 'object') return ''
  return [hints.read_only === true ? 'read-only' : '', hints.destructive === true ? 'destructive' : '',
    hints.idempotent === true ? 'idempotent' : '', hints.open_world === true ? 'reaches outside services' : ''].filter(Boolean).join(' · ')
}
