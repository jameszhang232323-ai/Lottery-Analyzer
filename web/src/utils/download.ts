/**
 * 浏览器文件下载：把已鉴权获取的 Blob 交给临时 `<a download>`。
 *
 * 关键边界：下载请求已通过同源 fetch 携带 HttpOnly Cookie，令牌不出现在 URL
 * 中；这里只处理已经拿到的 Blob，不发起网络请求。
 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename || 'download.bin'
  anchor.rel = 'noopener'
  anchor.style.display = 'none'
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}
