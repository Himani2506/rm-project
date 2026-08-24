/**
 * Score distribution with the current cutoff drawn through it.
 *
 * Bars at or above the threshold are filled in the accent colour, the rest
 * drain to grey, so dragging the slider shows the coordinator exactly how much
 * of the cohort a given cutoff admits. Hand-rolled SVG — no chart dependency.
 */
export default function Histogram({ buckets, threshold, max = 300 }) {
  const width = 100
  const height = 100
  const bucketWidth = 20

  if (!buckets?.length) {
    return <div className="empty" style={{ padding: '28px 0' }}>No distribution yet.</div>
  }

  const peak = Math.max(...buckets.map((b) => b.count), 1)
  const ceiling = Math.max(max, ...buckets.map((b) => b.bucket + bucketWidth))
  const x = (value) => (value / ceiling) * width
  const cutoffX = x(Math.min(threshold, ceiling))

  return (
    <svg
      className="histogram"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Score distribution. Cutoff at ${threshold}.`}
    >
      {buckets.map((b) => {
        const barHeight = (b.count / peak) * 74
        const included = b.bucket + bucketWidth > threshold
        return (
          <rect
            key={b.bucket}
            className={`bar${included ? ' bar--in' : ''}`}
            x={x(b.bucket) + 0.3}
            y={82 - barHeight}
            width={x(bucketWidth) - 0.6}
            height={Math.max(barHeight, 0.8)}
          />
        )
      })}

      <line x1="0" y1="82" x2={width} y2="82" stroke="var(--rule)" strokeWidth="0.4" />
      <line className="cutoff" x1={cutoffX} y1="2" x2={cutoffX} y2="82" vectorEffect="non-scaling-stroke" />
      <text
        className="cutoff-label"
        x={cutoffX > width - 14 ? cutoffX - 1 : cutoffX + 1.5}
        y="8"
        textAnchor={cutoffX > width - 14 ? 'end' : 'start'}
        style={{ fontSize: '5px' }}
      >
        {threshold}
      </text>

      {[0, 0.25, 0.5, 0.75, 1].map((f) => Math.round((ceiling * f) / 10) * 10).map((tick) => (
        <text
          key={tick}
          className="axis"
          x={x(tick) + (tick === 0 ? 0.5 : 0)}
          y="94"
          textAnchor={tick === 0 ? 'start' : 'middle'}
          style={{ fontSize: '5px' }}
        >
          {tick}
        </text>
      ))}
    </svg>
  )
}
