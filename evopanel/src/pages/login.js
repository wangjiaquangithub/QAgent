/**
 * 企业登录页 — 账号密码、二维码与企业 SSO 的统一认证入口。
 *
 * 当后端 WebUI 中间件检测到未认证的页面请求时，会 302 重定向到 /login。
 * QR 登录通过 /qr-login?token=xxx 自动提交。
 * OIDC 回调：`#/auth/callback?token=...&redirect=/chat`
 *
 * 路由注册在 main.js: registerRoute('/login', ...) + registerRoute('/qr-login', ...)
 */
import { version as APP_VERSION } from '../../package.json'
import { prepareAuthenticatedSession } from '../lib/account-session.js'
import {
  buildOidcLoginUrl,
  getWebuiStatus,
  login,
  qrLogin,
  saveAuthToken,
} from '../lib/webui-remote.js'

/** @type {HTMLElement | null} */
let _root = null

const MISSION_CONTROL_MARK = `<svg class="webui-login-mark" viewBox="0 0 44 44" fill="none" aria-hidden="true">
  <circle cx="22" cy="22" r="17" stroke="currentColor" stroke-width="1.5" opacity=".42"/>
  <path d="M28.2 29.1A10.2 10.2 0 1 1 31.1 22" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"/>
  <path d="M27.8 27.7 35 35" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"/>
  <circle cx="31.2" cy="21.8" r="2.7" fill="currentColor"/>
</svg> `

const QR_HINT_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><path d="M3 7V5a2 2 0 012-2h2M17 3h2a2 2 0 012 2v2M21 17v2a2 2 0 01-2 2h-2M7 21H5a2 2 0 01-2-2v-2"/><rect x="7" y="7" width="3" height="3" rx="0.5" fill="currentColor" stroke="none"/><rect x="14" y="7" width="3" height="3" rx="0.5" fill="currentColor" stroke="none"/><rect x="7" y="14" width="3" height="3" rx="0.5" fill="currentColor" stroke="none"/><path d="M14 14h3v3h-3z"/><path d="M14 17h3"/></svg>`

const SHIELD_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M12 3l8 3v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-3z"/><path d="M9 12l2 2 4-4"/></svg>`

function parseLoginQuery() {
  const hash = window.location.hash.slice(1)
  const queryStr = hash.includes('?') ? hash.split('?')[1] : ''
  return new URLSearchParams(queryStr || '')
}

function brandPanelHtml() {
  const title = '让工作，持续向前。'
  const lead = '把想法交给智能体，把注意力留给真正重要的工作。'
  return `
    <aside class="webui-login-brand-panel" aria-label="QAgent">
      <div class="webui-login-brand-inner">
        <div class="webui-login-brand-markrow">
          ${MISSION_CONTROL_MARK}
          <span class="webui-login-brand-word">QAgent</span>
          <span class="webui-login-brand-system">MISSION CONTROL</span>
        </div>
        <div class="webui-login-brand-copy">
          <p class="webui-login-brand-kicker">AI WORKBENCH</p>
          <h1 class="webui-login-brand-title">${title}</h1>
          <p class="webui-login-brand-lead">${lead}</p>
        </div>
      </div>
      <div class="webui-login-brand-signal" aria-hidden="true">
        <span>01</span><i></i><i></i><i></i><b>ONLINE</b>
      </div>
    </aside>`
}

/**
 * @param {{
 *   oidcEnabled?: boolean,
 *   oidcButtonLabel?: string,
 *   passwordLoginEnabled?: boolean,
 *   redirect?: string,
 *   error?: string,
 *   statusTitle?: string,
 *   statusBody?: string,
 * }} opts
 */
function loginPanelBody(opts) {
  const {
    oidcEnabled = false,
    oidcButtonLabel = '使用企业 SSO 登录',
    passwordLoginEnabled = true,
    error = '',
    statusTitle = '',
    statusBody = '',
  } = opts

  const eyebrow = 'WELCOME BACK'
  const title = '欢迎回来'
  const desc = '登录你的 QAgent 工作空间，继续推进正在进行的工作。'
  const usernameValue = ''
  const usernamePlaceholder = '输入用户名或邮箱'
  const showPassword = passwordLoginEnabled !== false
  const showSso = Boolean(oidcEnabled)

  if (statusTitle) {
    return `
      <div class="webui-login-panel-head">
        <p class="webui-login-eyebrow">${eyebrow}</p>
        <h2 class="webui-login-panel-title">${statusTitle}</h2>
        ${statusBody ? `<p class="webui-login-panel-desc">${statusBody}</p>` : ''}
      </div>
      ${opts.statusSlot || ''}`
  }

  const ssoBlock = showSso
    ? `<button type="button" id="login-sso" class="webui-login-sso">
         ${SHIELD_ICON}
         <span>${oidcButtonLabel}</span>
       </button>
       ${showPassword ? '<div class="webui-login-divider"><span>或使用账号密码</span></div>' : ''}`
    : ''

  const passwordBlock = showPassword
    ? `<form id="login-form" class="webui-login-form">
        <label class="webui-login-field">
          <span class="webui-login-label">用户名或邮箱</span>
          <input id="login-username" class="webui-login-input" type="text" placeholder="${usernamePlaceholder}" autocomplete="username" value="${usernameValue}" />
        </label>
        <label class="webui-login-field">
          <span class="webui-login-label">密码</span>
          <input id="login-password" class="webui-login-input" type="password" placeholder="请输入密码" autocomplete="current-password" />
        </label>
        <button type="submit" id="login-submit" class="webui-login-submit">进入工作空间</button>
      </form>`
    : ''

  const footer = showPassword
    ? `<footer class="webui-login-footer">
        ${QR_HINT_ICON}
        <span>也可在桌面端「设置 → 远程访问」使用二维码登录</span>
      </footer>`
    : `<footer class="webui-login-footer">
        ${SHIELD_ICON}
        <span>本组织已启用企业身份认证，请使用上方入口登录</span>
      </footer>`

  return `
    <div class="webui-login-panel-head">
      <p class="webui-login-eyebrow">${eyebrow}</p>
      <h2 class="webui-login-panel-title">${title}</h2>
      <p class="webui-login-panel-desc">${desc}</p>
    </div>
    ${ssoBlock}
    ${passwordBlock}
    <div id="login-error" class="webui-login-error" ${error ? '' : 'hidden'}>${error || ''}</div>
    ${footer}
    <div class="webui-login-version">QAgent v${APP_VERSION}</div>`
}

function loginHtml(opts) {
  return `
  <div class="webui-login-page">
    <div class="webui-login-layout">
      ${brandPanelHtml()}
      <section class="webui-login-panel" aria-label="登录表单">
        ${loginPanelBody(opts)}
      </section>
    </div>
  </div>`
}

function qrLoginHtml() {
  return loginHtml({
    statusTitle: '二维码登录',
    statusBody: '正在核验企业访问凭证，请稍候。',
    statusSlot: `
      <div id="qr-login-status" class="webui-login-qr-status">
        <div class="webui-login-spinner" aria-hidden="true"></div>
        <p class="webui-login-qr-text">正在验证…</p>
      </div>
      <div id="qr-login-error" class="webui-login-qr-error" hidden>
        <p class="webui-login-qr-error-title">登录失败</p>
        <p id="qr-login-error-msg" class="webui-login-qr-error-msg"></p>
        <a href="#/login" class="webui-login-link-btn">使用密码登录</a>
      </div>`,
  })
}

function authCallbackHtml() {
  return loginHtml({
    statusTitle: '正在完成登录',
    statusBody: '企业身份已确认，正在建立本地会话。',
    statusSlot: `
      <div class="webui-login-qr-status">
        <div class="webui-login-spinner" aria-hidden="true"></div>
        <p class="webui-login-qr-text">正在保存会话…</p>
      </div>`,
  })
}

function showError(msg) {
  const el = document.getElementById('login-error')
  if (!el) return
  if (msg) {
    el.textContent = msg
    el.hidden = false
  } else {
    el.textContent = ''
    el.hidden = true
  }
}

/**
 * @param {HTMLElement} root
 * @param {{ redirect?: string }} opts
 */
function wireLoginEvents(root, opts) {
  const { redirect = '/chat' } = opts

  root.querySelector('#login-sso')?.addEventListener('click', async () => {
    try {
      window.location.href = await buildOidcLoginUrl({ redirect })
    } catch (err) {
      showError(err?.message || '无法启动 SSO 登录')
    }
  })

  const form = root.querySelector('#login-form')
  form?.addEventListener('submit', async (e) => {
    e.preventDefault()
    const usernameRaw = root.querySelector('#login-username')?.value?.trim() || ''
    const username = usernameRaw
    const password = root.querySelector('#login-password')?.value || ''
    const submitBtn = root.querySelector('#login-submit')

    if (!username) {
      showError('请输入用户名')
      root.querySelector('#login-username')?.focus()
      return
    }
    if (!password) {
      showError('请输入密码')
      return
    }

    if (submitBtn) {
      submitBtn.disabled = true
      submitBtn.textContent = '正在进入…'
    }
    showError('')

    try {
      const resp = await login({ username, password })
      prepareAuthenticatedSession(resp.token)
      saveAuthToken(resp.token)
      const target = redirect.startsWith('/') ? redirect : `/${redirect}`
      window.location.hash = target
      window.location.reload()
    } catch (err) {
      showError(err?.message || '登录失败，请检查用户名和密码')
      if (submitBtn) {
        submitBtn.disabled = false
        submitBtn.textContent = '进入工作空间'
      }
    }
  })
}

async function handleQrLogin(root, token) {
  const statusEl = root.querySelector('#qr-login-status')
  const errorEl = root.querySelector('#qr-login-error')
  const errorMsgEl = root.querySelector('#qr-login-error-msg')

  try {
    const resp = await qrLogin({ qr_token: token })
    prepareAuthenticatedSession(resp.token)
    saveAuthToken(resp.token)
    window.location.hash = '/chat'
    window.location.reload()
  } catch (err) {
    if (statusEl) statusEl.hidden = true
    if (errorEl) errorEl.hidden = false
    if (errorMsgEl) errorMsgEl.textContent = err?.message || '二维码已过期或无效'
  }
}

function handleAuthCallback(params) {
  const token = params.get('token')
  const redirect = params.get('redirect') || '/chat'
  const err = params.get('error')

  if (err) {
    window.location.hash = `/login?error=${encodeURIComponent(err)}`
    window.location.reload()
    return
  }

  if (!token) {
    window.location.hash = '/login?error=missing_token'
    window.location.reload()
    return
  }

  prepareAuthenticatedSession(token)
  saveAuthToken(token)
  const target = redirect.startsWith('/') ? redirect : `/${redirect}`
  window.location.hash = target
  window.location.reload()
}

export function cleanup() {
  _root = null
}

/**
 * @param {{ mode?: 'login' | 'qr-login' | 'auth-callback', qrToken?: string }} [opts]
 * @returns {HTMLElement}
 */
export function render(opts) {
  cleanup()
  const page = document.createElement('div')
  _root = page

  let mode = opts?.mode || 'login'
  let qrToken = opts?.qrToken || null

  if (!opts) {
    const hash = window.location.hash.slice(1)
    const [path, queryStr] = hash.split('?')
    if (path === '/qr-login') {
      mode = 'qr-login'
      qrToken = new URLSearchParams(queryStr || '').get('token')
    } else if (path === '/auth/callback') {
      mode = 'auth-callback'
    }
  }

  const params = parseLoginQuery()
  const redirect = params.get('redirect') || '/chat'
  const error = params.get('error') || ''

  if (mode === 'auth-callback') {
    page.innerHTML = authCallbackHtml()
    queueMicrotask(() => handleAuthCallback(params))
    return page
  }

  if (mode === 'qr-login') {
    page.innerHTML = qrLoginHtml()
    if (qrToken) void handleQrLogin(page, qrToken)
    else {
      const statusEl = page.querySelector('#qr-login-status')
      const errorEl = page.querySelector('#qr-login-error')
      const errorMsgEl = page.querySelector('#qr-login-error-msg')
      if (statusEl) statusEl.hidden = true
      if (errorEl) errorEl.hidden = false
      if (errorMsgEl) errorMsgEl.textContent = '缺少二维码 token'
    }
  } else {
    page.innerHTML = loginHtml({
      redirect,
      error,
    })
    wireLoginEvents(page, { redirect })
    void getWebuiStatus()
      .then((status) => {
        if (!page.isConnected) return
        page.innerHTML = loginHtml({
          redirect,
          error,
          oidcEnabled: Boolean(status?.oidc_enabled),
          oidcButtonLabel: status?.oidc_button_label || '使用企业 SSO 登录',
          passwordLoginEnabled: status?.password_login_enabled !== false,
        })
        wireLoginEvents(page, { redirect })
        queueMicrotask(() => {
          page.querySelector('#login-username')?.focus()
        })
      })
      .catch(() => {
        queueMicrotask(() => {
          page.querySelector('#login-username')?.focus()
        })
      })
  }

  return page
}
