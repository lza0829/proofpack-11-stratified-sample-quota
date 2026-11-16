async function parseError(r) {
  const text = await r.text()
  try {
    const data = JSON.parse(text)
    if (typeof data.detail === 'string') return data.detail
    if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg || JSON.stringify(d)).join('；')
  } catch { /* 非 JSON，原样返回 */ }
  return text
}

async function send(url, method, body) {
  const r = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!r.ok) throw new Error(await parseError(r))
  return r.json()
}

export async function getJSON(url) {
  const r = await fetch(url)
  if (!r.ok) throw new Error(await parseError(r))
  return r.json()
}

export async function postJSON(url, body = {}) {
  return send(url, 'POST', body)
}

export async function putJSON(url, body = {}) {
  return send(url, 'PUT', body)
}
