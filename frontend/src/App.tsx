import { ChangeEvent, DragEvent, useEffect, useMemo, useState, useRef } from 'react'
import { AlertTriangle, Check, FileImage, LoaderCircle, RotateCcw, ScanLine, Upload } from 'lucide-react'

type Assignment = {
  time: string
  shift: string
  cell_text: string
  column_header_evidence: string
  confidence: number
  note: string
  needs_work: boolean
}

type ScheduleResult = {
  document_date: string | null
  employee_name: string
  employee_code: string
  assignments: Assignment[]
  is_day_off: boolean
  working_ranges: string[]
  shift_slots: string[]
  warnings: string[]
  preprocessing: { method: string }
  roster_profile: 'international' | 'domestic_control'
  audit: {
    expected_total: number
    expected_working: number | null
    expected_resting: number | null
    actual_total: number
    actual_working: number
    actual_resting: number
    passed: boolean
  } | null
  usage?: {
    api_calls: number
    input_tokens: number
    output_tokens: number
    estimated_usd: number | null
  }
  usage_lifetime?: {
    known_estimated_usd: number
    unpriced_calls: number
  }
  timing?: {
    preprocessing_seconds: number
    total_seconds: number
  }
}

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

async function responsePayload(response: Response): Promise<Record<string, unknown> | null> {
  const body = await response.text()
  if (!body.trim()) return null
  try {
    return JSON.parse(body) as Record<string, unknown>
  } catch {
    return null
  }
}

const demoResult: ScheduleResult = {
  document_date: '2026-09-07',
  employee_name: '蕭志明',
  employee_code: '己',
  assignments: [
    { time: '07-08', shift: '國際線', cell_text: '己', column_header_evidence: '國際線', confidence: 0.96, note: '', needs_work: true },
    { time: '08-09', shift: '國際線', cell_text: '己', column_header_evidence: '國際線', confidence: 0.93, note: '', needs_work: true },
    { time: '09-10', shift: '國際線（二）', cell_text: '己', column_header_evidence: '國際線（二）', confidence: 0.91, note: '', needs_work: false },
    { time: '14-15', shift: '國內線一線', cell_text: '己', column_header_evidence: '國內線一線', confidence: 0.76, note: '格線附近字跡較淡', needs_work: true },
  ],
  is_day_off: false,
  working_ranges: ['07:00–09:00', '14:00–15:00'],
  shift_slots: ['07-08', '08-09', '09-10', '10-11', '11-12', '12-13', '13-14', '14-15'],
  warnings: ['14-15 的代號較淡，請對照原表確認'],
  preprocessing: { method: 'full_photo_strips' },
  roster_profile: 'international',
  audit: null,
  usage: { api_calls: 1, input_tokens: 0, output_tokens: 0, estimated_usd: null },
  usage_lifetime: { known_estimated_usd: 0, unpriced_calls: 0 },
  timing: { preprocessing_seconds: 0, total_seconds: 0 },
}

function hourStart(time: string) {
  const value = Number(time.slice(0, 2))
  return Number.isFinite(value) ? value : 5
}

export default function App() {
  const viewer = useRef<HTMLDialogElement>(null)
  const [zoom, setZoom] = useState(100)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState('')
  const [dragging, setDragging] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<ScheduleResult | null>(null)

  useEffect(() => {
    if (!file) return
    const url = URL.createObjectURL(file)
    setPreview(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const sortedAssignments = useMemo(
    () => [...(result?.assignments ?? [])].sort((a, b) => hourStart(a.time) - hourStart(b.time)),
    [result],
  )

  function acceptFile(candidate?: File) {
    if (!candidate) return
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(candidate.type)) {
      setError('請選擇 JPG、PNG 或 WEBP 班表照片。')
      return
    }
    if (candidate.size > 15 * 1024 * 1024) {
      setError('圖片不可超過 15 MB。')
      return
    }
    setError('')
    setResult(null)
    setFile(candidate)
  }

  function onDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault()
    setDragging(false)
    acceptFile(event.dataTransfer.files[0])
  }

  async function analyze() {
    if (!file) return
    setLoading(true)
    setError('')
    const form = new FormData()
    form.append('file', file)
    try {
      const response = await fetch(`${API_BASE}/api/analyze`, { method: 'POST', body: form })
      const payload = await responsePayload(response)
      if (!response.ok) {
        const detail = typeof payload?.detail === 'string' ? payload.detail : ''
        throw new Error(
          detail || '無法連接後端 API。請先執行 uvicorn backend.app:app --reload --port 8010。',
        )
      }
      if (!payload) throw new Error('後端沒有回傳有效資料，請查看後端終端機的錯誤訊息。')
      setResult(payload as unknown as ScheduleResult)
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : ''
      setError(
        message === 'Failed to fetch'
          ? '無法連接後端 API。請先執行 uvicorn backend.app:app --reload --port 8010。'
          : message || '無法完成辨識，請稍後再試。',
      )
    } finally {
      setLoading(false)
    }
  }

  function reset() {
    setFile(null)
    setPreview('')
    setResult(null)
    setError('')
  }

  return (
    <main className="shell">
      <header className="masthead">
        <div className="identity-mark" aria-hidden="true">己</div>
        <div>
          <h1>勤務時間簿</h1>
          <p>蕭志明的每日班表辨識</p>
        </div>
        <div className="date-stamp">{new Intl.DateTimeFormat('zh-TW', { dateStyle: 'long' }).format(new Date())}</div>
      </header>

      <div className="rule" />

      <section className="workspace">
        <div className="scan-column">
          <div className="section-heading">
            <div>
              <h2>放入今天的班表</h2>
              <p>照片越正、文字越清楚，辨識會越準確。</p>
            </div>
            {file && <button className="quiet-button" onClick={reset}><RotateCcw size={16} />重選</button>}
          </div>

          <label
            className={`dropzone ${dragging ? 'is-dragging' : ''} ${preview ? 'has-image' : ''}`}
            onDragOver={(event) => { event.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <input type="file" accept="image/jpeg,image/png,image/webp" onChange={(event: ChangeEvent<HTMLInputElement>) => acceptFile(event.target.files?.[0])} />
            {preview ? (
              <img src={preview} alt="準備辨識的班表預覽" />
            ) : (
              <div className="drop-prompt">
                <div className="scan-corners"><ScanLine size={42} /></div>
                <strong>拖曳照片到這裡</strong>
                <span>或點一下選擇班表照片</span>
                <small>JPG、PNG、WEBP · 上限 15 MB</small>
              </div>
            )}
          </label>

          {preview && <button className="quiet-button" onClick={() => { setZoom(100); viewer.current?.showModal() }}>🔍 放大檢視照片</button>}
          <dialog ref={viewer} className="photo-viewer" aria-label="班表照片放大檢視">
            <div className="photo-tools">
              <button onClick={() => setZoom(value => Math.max(50, value - 25))} disabled={zoom <= 50}>縮小 −</button>
              <output>{zoom}%</output>
              <button onClick={() => setZoom(value => Math.min(400, value + 25))} disabled={zoom >= 400}>放大 ＋</button>
              <button onClick={() => setZoom(100)}>重設</button>
              <button onClick={() => viewer.current?.close()}>關閉 ✕</button>
            </div>
            <div className="photo-scroll">{preview && <img src={preview} alt="可放大的原始班表照片" style={{ width: `${zoom}%` }} />}</div>
          </dialog>

          {error && <div className="error-message" role="alert"><AlertTriangle size={18} />{error}</div>}

          <button className="analyze-button" disabled={!file || loading} onClick={analyze}>
            {loading ? <LoaderCircle className="spin" size={20} /> : <Upload size={20} />}
            {loading ? '正在校準格線並辨識…' : '讀取我的工作時間'}
          </button>

          {!result && !file && (
            <button className="demo-link" onClick={() => setResult(demoResult)}>先看看結果長什麼樣子</button>
          )}
        </div>

        <div className="result-column" aria-live="polite">
          {result ? (
            <>
              <div className="result-header">
                <div>
                  <span>{result.document_date ?? '日期待確認'}</span>
                  <h2>{result.employee_name} <b>{result.employee_code}</b></h2>
                </div>
                <div className="verified"><Check size={16} />{result.is_day_off ? '當日休假' : result.audit?.passed ? '已辨識 8 個時段' : '辨識結果待核對'}</div>
              </div>

              {result.is_day_off ? (
                <section className="work-summary"><p>當日休假</p><strong>沒有排入任何時段</strong></section>
              ) : (
                <section className="work-summary">
                  <p>真正需要工作的時段</p>
                  {result.working_ranges.length ? (
                    result.working_ranges.map((range) => <strong key={range}>{range}</strong>)
                  ) : <strong>尚未辨識到工作時段</strong>}
                </section>
              )}

              {result.audit && (
                <section className={`audit ${result.audit.passed ? 'passed' : 'failed'}`}>
                  <h3>{result.audit.passed ? '時段數量完整' : '時段尚未完整'}</h3>
                  <p>辨識到 {result.audit.actual_total} 格：{result.audit.actual_working} 小時工作、{result.audit.actual_resting} 小時休息。</p>
                  <small>檢查 8 個不重複時段；工作與休息比例依原表，不強制 6 工 2 休。</small>
                </section>
              )}

              <section className="timeline-section">
                <div className="timeline-title">
                  <h3>實際班表</h3>
                  <div className="legend"><span className="work-dot" />上班 <span className="rest-dot" />休息</div>
                </div>
                {sortedAssignments.length ? (
                  <ol className="timeline">
                    {sortedAssignments.map((item, index) => {
                      const uncertain = item.confidence < 0.8 || Boolean(item.note)
                      return (
                        <li key={`${item.time}-${item.shift}-${index}`} className={item.needs_work ? 'work' : 'rest'}>
                          <time>{item.time.replace(/(\d{2})-(\d{2})/, '$1:00–$2:00')}</time>
                          <div className="timeline-line"><span /></div>
                          <div className="shift-detail">
                            <strong>{item.shift}</strong>
                            <span>{item.needs_work ? '需要工作' : '休息'}</span>
                            {item.column_header_evidence && <small className="header-evidence">表頭：{item.column_header_evidence}</small>}
                            {uncertain && <em><AlertTriangle size={14} />{item.note || '請核對原表'}</em>}
                          </div>
                        </li>
                      )
                    })}
                  </ol>
                ) : <div className="empty-result">當日休假，沒有排入任何時段。</div>}
              </section>

              {result.warnings.length > 0 && (
                <section className="warnings">
                  <h3><AlertTriangle size={17} />請人工確認</h3>
                  {result.warnings.map((warning) => <p key={warning}>{warning}</p>)}
                </section>
              )}

              {result.usage && result.timing && result.usage_lifetime && (
                <section className="recognition-metrics">
                  <h3>本次辨識統計</h3>
                  <p>耗時 {result.timing.total_seconds.toFixed(1)} 秒（圖片前處理 {result.timing.preprocessing_seconds.toFixed(1)} 秒）</p>
                  <p>API {result.usage.api_calls} 次｜輸入 {result.usage.input_tokens.toLocaleString()}／輸出 {result.usage.output_tokens.toLocaleString()} tokens</p>
                  <p>本次估計費用：{result.usage.estimated_usd === null ? '費率或用量不足，無法估算' : `US$${result.usage.estimated_usd.toFixed(6)}`}</p>
                  <p>開始記錄以來累計：US${result.usage_lifetime.known_estimated_usd.toFixed(6)}{result.usage_lifetime.unpriced_calls ? '（部分呼叫無法計價）' : ''}</p>
                </section>
              )}

              <footer className="process-note">
                <FileImage size={15} />
                已使用完整原圖與逐時放大圖辨識
              </footer>
            </>
          ) : (
            <div className="empty-state">
              <div className="empty-grid" aria-hidden="true" />
              <ScanLine size={42} />
              <h2>結果會在這裡排成時間軸</h2>
              <p>完整保留五種班別，同時清楚標出真正需要工作的時段。</p>
            </div>
          )}
        </div>
      </section>
    </main>
  )
}
