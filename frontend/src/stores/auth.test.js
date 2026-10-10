import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'
import { decodeJwtExp } from '../utils/jwt'
import { BizError } from '../api/errors'

const MIN = 60 * 1000

/** 手工拼一个 exp 可控的假 JWT（前端只解码不验签） */
function makeToken(expSec) {
  const b64url = (obj) => btoa(JSON.stringify(obj)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `${b64url({ alg: 'HS256', typ: 'JWT' })}.${b64url({ sub: '42', username: 'alice', typ: 'access', exp: expSec })}.sig`
}

describe('decodeJwtExp', () => {
  it('解析 exp 为毫秒', () => {
    expect(decodeJwtExp(makeToken(1791660153))).toBe(1791660153000)
  })
  it('畸形 token 返回 null', () => {
    expect(decodeJwtExp('not-a-jwt')).toBeNull()
    expect(decodeJwtExp('')).toBeNull()
  })
})

describe('auth store exp 预检（§4.3 剩 <5min 先刷新再放行）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('剩 10min：不触发刷新直接放行', async () => {
    const auth = useAuthStore()
    auth.accessToken = makeToken(Math.floor((Date.now() + 10 * MIN) / 1000))
    auth.expAt = decodeJwtExp(auth.accessToken)
    const spy = vi.spyOn(auth, 'refresh').mockResolvedValue({})
    await auth.ensureFreshToken()
    expect(spy).not.toHaveBeenCalled()
  })

  it('剩 4min：先刷新再放行', async () => {
    const auth = useAuthStore()
    auth.accessToken = makeToken(Math.floor((Date.now() + 4 * MIN) / 1000))
    auth.expAt = decodeJwtExp(auth.accessToken)
    const spy = vi.spyOn(auth, 'refresh').mockImplementation(async () => {
      auth.accessToken = makeToken(Math.floor((Date.now() + 120 * MIN) / 1000))
      auth.expAt = decodeJwtExp(auth.accessToken)
    })
    await auth.ensureFreshToken()
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('exp 不可解析（0）：保守刷新', async () => {
    const auth = useAuthStore()
    auth.accessToken = 'garbage'
    auth.expAt = 0
    const spy = vi.spyOn(auth, 'refresh').mockResolvedValue({})
    await auth.ensureFreshToken()
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('未登录：抛 auth_required', async () => {
    const auth = useAuthStore()
    await expect(auth.ensureFreshToken()).rejects.toMatchObject({ code: 'auth_required' })
  })
})

describe('auth store refresh（旋转换发）', () => {
  beforeEach(() => setActivePinia(createPinia()))
  afterEach(() => vi.restoreAllMocks())

  it('成功：setSession 更新双 token/user/exp', async () => {
    const auth = useAuthStore()
    auth.refreshToken = 'old-refresh'
    const newAccess = makeToken(Math.floor((Date.now() + 120 * MIN) / 1000))
    vi.spyOn(axios, 'post').mockResolvedValue({
      data: {
        code: 'ok',
        data: { accessToken: newAccess, refreshToken: 'new-refresh', user: { id: 42, username: 'alice', hasKey: true } },
      },
    })
    await auth.refresh()
    expect(auth.accessToken).toBe(newAccess)
    expect(auth.refreshToken).toBe('new-refresh')
    expect(auth.user).toMatchObject({ id: 42, hasKey: true })
    expect(auth.expAt).toBe(decodeJwtExp(newAccess))
  })

  it('失败：清会话并抛 invalid_refresh', async () => {
    const auth = useAuthStore()
    auth.accessToken = 'a'
    auth.refreshToken = 'r'
    auth.user = { id: 1 }
    vi.spyOn(axios, 'post').mockRejectedValue(new Error('boom'))
    const err = await auth.refresh().catch((e) => e)
    expect(err).toBeInstanceOf(BizError)
    expect(err.code).toBe('invalid_refresh')
    expect(auth.accessToken).toBe('')
    expect(auth.refreshToken).toBe('')
    expect(auth.user).toBeNull()
  })

  it('信封 code 非 ok：同样按 invalid_refresh 清会话', async () => {
    const auth = useAuthStore()
    auth.refreshToken = 'expired'
    vi.spyOn(axios, 'post').mockResolvedValue({ data: { code: 'invalid_refresh', message: 'expired' } })
    const err = await auth.refresh().catch((e) => e)
    expect(err.code).toBe('invalid_refresh')
    expect(auth.refreshToken).toBe('')
  })

  it('ensureFresh 单飞：并发调用只发一次 refresh', async () => {
    const auth = useAuthStore()
    auth.accessToken = makeToken(Math.floor((Date.now() + 1 * MIN) / 1000))
    auth.expAt = decodeJwtExp(auth.accessToken)
    let calls = 0
    vi.spyOn(auth, 'refresh').mockImplementation(
      () =>
        new Promise((resolve) => {
          calls += 1
          setTimeout(resolve, 20)
        })
    )
    await Promise.all([auth.ensureFresh(), auth.ensureFresh(), auth.ensureFresh()])
    expect(calls).toBe(1)
  })
})
