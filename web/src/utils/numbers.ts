/**
 * 号码与参数解析（纯函数）。
 *
 * 这些工具只做输入格式收敛，最终白名单与范围校验仍由后端完成；解析失败时
 * 返回空数组或 null，界面据此提示，不静默改写用户输入。
 */

/** 把“1,2, 3”或“1 2 3”解析为去重升序整数；非法片段被忽略。 */
export function parseNumberList(text: string): number[] {
  const values = text
    .split(/[\s,，、;；]+/)
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => Number(part))
    .filter((value) => Number.isInteger(value))
  return [...new Set(values)].sort((left, right) => left - right)
}

/** 解析可选整数列表：空白输入返回 null（表示“不限制”）。 */
export function parseOptionalNumberList(text: string): number[] | null {
  if (!text.trim()) return null
  return parseNumberList(text)
}

function parseInteger(text: string): number | null {
  if (!text.trim()) return null
  const value = Number(text.trim())
  return Number.isInteger(value) ? value : null
}

/** 解析和值闭区间；任一输入为空返回 null。 */
export function parseRangePair(lowText: string, highText: string): [number, number] | null {
  const low = parseInteger(lowText)
  const high = parseInteger(highText)
  if (low === null || high === null) return null
  return [low, high]
}

/** 解析三区个数；必须恰好 3 个整数（允许重复，例如 2,2,2）。 */
export function parseTriple(text: string): [number, number, number] | null {
  const values = text
    .split(/[\s,，、;；]+/)
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => Number(part))
    .filter((value) => Number.isInteger(value))
  if (values.length !== 3) return null
  return [values[0], values[1], values[2]]
}

/** 解析可选整数；空白返回 null。 */
export function parseOptionalInteger(text: string): number | null {
  return parseInteger(text)
}

/**
 * 生成 64 位十六进制随机种子。
 *
 * 使用浏览器 Web Crypto；种子只用于复现候选，不包含凭证。没有 Web Crypto
 * 的运行环境才退回 Math.random（仅测试/旧浏览器）。
 */
export function randomSeedHex(): string {
  const bytes = new Uint8Array(32)
  const webCrypto = typeof globalThis.crypto === 'undefined' ? null : globalThis.crypto
  if (webCrypto && typeof webCrypto.getRandomValues === 'function') {
    webCrypto.getRandomValues(bytes)
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256)
    }
  }
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/** 校验种子是否为 64 位十六进制字符串。 */
export function isSeedHex(value: string): boolean {
  return /^[0-9a-fA-F]{64}$/.test(value)
}
