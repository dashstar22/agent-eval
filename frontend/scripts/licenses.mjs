import fs from 'node:fs'
import path from 'node:path'
const lock = JSON.parse(fs.readFileSync('package-lock.json', 'utf8'))
const items = Object.entries(lock.packages).filter(([key]) => key).map(([key, item]) => {
  const file = path.join(key, 'package.json')
  const installed = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {}
  return { name: installed.name || key.split('node_modules/').pop(), version: item.version, license: installed.license || item.license || 'UNKNOWN', dev: item.dev || false, repository: installed.repository?.url || installed.repository || null }
})
fs.writeFileSync('dependency-licenses.json', JSON.stringify({ generated_on: '2026-11-16', source: 'package-lock.json 与已安装包的 package.json；安装时读取，不代表源码许可审计', packages: items }, null, 2) + '\n')
console.log(JSON.stringify({ packages: items.length, unknown: items.filter(item => item.license === 'UNKNOWN').map(item => item.name) }))
