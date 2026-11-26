/** cc10050 迁移场景接口封装：全部返回 any，由各面板做防御式渲染。 */
import { request } from './client'

function withApi(path: string): string {
  if (path.startsWith('/api/')) return path
  return `/api${path.startsWith('/') ? path : `/${path}`}`
}

export async function scenarioGet(path: string): Promise<any> {
  const response = await request(withApi(path))
  let data: any = null
  try {
    data = await response.json()
  } catch {
    data = { ok: response.ok, message: '响应不是 JSON' }
  }
  if (!response.ok && data && typeof data === 'object') {
    data.__httpStatus = response.status
  }
  return data
}

export async function scenarioPost(path: string, body: Record<string, any> = {}): Promise<any> {
  const response = await request(withApi(path), {
    method: 'POST',
    body: JSON.stringify(body),
  })
  let data: any = null
  try {
    data = await response.json()
  } catch {
    data = { ok: response.ok, message: '响应不是 JSON' }
  }
  if (!response.ok && data && typeof data === 'object') {
    data.__httpStatus = response.status
  }
  return data
}

export const q = (params: Record<string, string | number | undefined | null>): string => {
  const usp = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') usp.set(key, String(value))
  })
  const text = usp.toString()
  return text ? `?${text}` : ''
}
