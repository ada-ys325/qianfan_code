package operator

import (
	"context"
	"encoding/json"
	"regexp"
	"strings"
	"time"

	"icode.baidu.com/baidu/easydata/reflow-go/pkg/logger"
	"icode.baidu.com/baidu/easydata/reflow-go/pkg/metrics"
)

// ────────────────────────────────────────────────────────────────────────────
// Constants & package-level data
// ────────────────────────────────────────────────────────────────────────────

const (
	maskValue                = "***"
	desensitizeRuleHitsField = "desensitize_rule_hits"
	secretKeyRule            = "secret_key"
)

var defaultWhitelistFields = map[string]struct{}{
	"account_id":   {},
	"account_type": {},
	"appid_v2":     {},
	"cloud_id":     {},
	"url":          {},
	"as_id":        {},
	"request_id":   {},
}

type secretPattern struct {
	re              *regexp.Regexp
	secretGroupIdxs []int // 1-based group indices whose content should be masked
}

var secretPatterns = []secretPattern{
	{regexp.MustCompile(`(?i)([^0-9A-Za-z]|^)(LTAI[a-z0-9]{20})([^0-9A-Za-z]|$)`), []int{2}},
	{regexp.MustCompile(`(?i)(alibaba[a-z0-9_ .\-,]{0,25})(=|>|:=|\|\|:|<=|=>|:).{0,5}['"]([a-z0-9]{30})['"]`), []int{3}},
	{regexp.MustCompile(`((A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16})`), []int{1}},
	{regexp.MustCompile(`(?i)(aws)?_?(secret)?_?(access)?_?key.{0,5}['"]([A-Za-z0-9/+=]{40})['"]`), []int{4}},
	{regexp.MustCompile(`(?i)\b([0-9a-f]{12}4[0-9a-f]{19}|ALTAK[a-z0-9]{21})\b`), []int{1}},
	{regexp.MustCompile(`(?i)(ACCESS_KEY_SECRET|secret[-_.]?(access)?(key)?|sk)[ \t'"]*(?:[:=]|=>)[ \t]*['"']?([0-9A-Za-z]{32})(?:[^a-zA-Z0-9(]|$)`), []int{4}},
	{regexp.MustCompile(`(?i)(^|[^0-9A-Za-z_-])(sk-[A-Za-z0-9][A-Za-z0-9_-]{20,})([^0-9A-Za-z_-]|$)`), []int{2}},
	{regexp.MustCompile(`(bce-v3/)(ALTAK-[A-Za-z0-9]{21})(/)([a-z0-9]{40})`), []int{2, 4}},
	{regexp.MustCompile(`(?i)(bce-v[1-3]/)(ALTAK-[A-Za-z0-9_-]{8,})(/)([A-Za-z0-9]{20,})`), []int{2, 4}},
	{regexp.MustCompile(`(?i)(^|[^0-9A-Za-z_-])(ALTAK-[a-z0-9]{21})([^0-9A-Za-z_-]|$)`), []int{2}},
	{regexp.MustCompile(`(?i)(^|[^0-9A-Za-z])([0-9a-f]{40})([^0-9A-Za-z]|$)`), []int{2}},
}

// ────────────────────────────────────────────────────────────────────────────
// Public types
// ────────────────────────────────────────────────────────────────────────────

// MaskStats records how many secret values were masked and how often secret patterns matched.
type MaskStats struct {
	MaskedSecrets int64
	RuleHits      map[string]int
}

func (s *MaskStats) addRuleHit() {
	if s.RuleHits == nil {
		s.RuleHits = make(map[string]int)
	}
	s.RuleHits[secretKeyRule]++
}

// ────────────────────────────────────────────────────────────────────────────
// Operator
// ────────────────────────────────────────────────────────────────────────────

// StringDesensitize is a pipeline operator that scans every string value in a
// JSON document and replaces known secret patterns with "***".
// Whitelisted field keys (exact match or dot-path match) are left untouched.
//
// If the input cannot be parsed as JSON it is treated as a plain string and
// the patterns are applied directly.
//
// The operator implements pipeline.Operator[[]byte, []byte].
type StringDesensitize struct {
	whitelistFields map[string]struct{}
	metrics         *metrics.DesensitizeMetrics // nil = noop
}

// NewStringDesensitize creates the operator.
// extraFields may be:
//   - nil / empty string → only the built-in default whitelist is used
//   - a JSON array string  → e.g. `["request_id","token"]`
//   - a comma-separated string → e.g. `"request_id,token"`
//
// m may be nil; in that case metrics reporting is skipped (noop).
func NewStringDesensitize(extraFields string, m *metrics.DesensitizeMetrics) *StringDesensitize {
	wl := parseWhitelistFields(extraFields)
	for k := range defaultWhitelistFields {
		wl[k] = struct{}{}
	}
	return &StringDesensitize{whitelistFields: wl, metrics: m}
}

// Process implements pipeline.Operator[[]byte, []byte].
func (op *StringDesensitize) Process(_ context.Context, in []byte) ([]byte, bool, error) {
	if len(in) == 0 {
		return in, false, nil
	}

	start := time.Now()
	out, stats := MaskJSONBytes(in, op.whitelistFields)

	if m := op.metrics; m != nil {
		m.ProcessDuration.Observe(time.Since(start).Seconds())
		m.Total.Add(1)
		if stats.MaskedSecrets > 0 {
			m.MaskedTotal.Add(1)
		}
	}

	return out, false, nil
}

// ────────────────────────────────────────────────────────────────────────────
// Helpers — exported for unit tests
// ────────────────────────────────────────────────────────────────────────────

// MaskJSONBytes masks a JSON byte slice and returns (maskedBytes, stats).
func MaskJSONBytes(data []byte, whitelistFields map[string]struct{}) ([]byte, MaskStats) {
	stats := &MaskStats{}
	var doc map[string]interface{}
	if err := json.Unmarshal(data, &doc); err != nil {
		masked := maskString(string(data), stats)
		return []byte(masked), *stats
	}
	changed := maskMapIface(doc, "", whitelistFields, stats)
	if !changed {
		return data, *stats
	}
	doc[desensitizeRuleHitsField] = stats.RuleHits
	out, err := json.Marshal(doc)
	if err != nil {
		logger.Errorf("string_desensitize: marshal failed err=%v", err)
		return data, *stats
	}
	return out, *stats
}

// CreateWhitelistFields merges extraFields with the default whitelist.
func CreateWhitelistFields(extraFields string) map[string]struct{} {
	wl := parseWhitelistFields(extraFields)
	for k := range defaultWhitelistFields {
		wl[k] = struct{}{}
	}
	return wl
}

// ────────────────────────────────────────────────────────────────────────────
// Internal traversal
// ────────────────────────────────────────────────────────────────────────────

func maskMapIface(m map[string]interface{}, parentPath string, wl map[string]struct{}, stats *MaskStats) bool {
	changed := false
	for k, v := range m {
		currentPath := appendPath(parentPath, k)
		if isWhitelisted(wl, currentPath, k) {
			continue
		}
		if c := maskValueNode(v, currentPath, wl, stats); c != nil {
			m[k] = c
			changed = true
		}
	}
	return changed
}

func maskSliceIface(s []interface{}, parentPath string, wl map[string]struct{}, stats *MaskStats) bool {
	changed := false
	for i, v := range s {
		if c := maskValueNode(v, parentPath, wl, stats); c != nil {
			s[i] = c
			changed = true
		}
	}
	return changed
}

func maskValueNode(v interface{}, currentPath string, wl map[string]struct{}, stats *MaskStats) interface{} {
	switch tv := v.(type) {
	case string:
		before := stats.MaskedSecrets
		masked := maskString(tv, stats)
		if stats.MaskedSecrets != before {
			return masked
		}
	case map[string]interface{}:
		if maskMapIface(tv, currentPath, wl, stats) {
			return tv
		}
	case []interface{}:
		if maskSliceIface(tv, currentPath, wl, stats) {
			return tv
		}
	}
	return nil
}

// ────────────────────────────────────────────────────────────────────────────
// String-level masking
// ────────────────────────────────────────────────────────────────────────────

func maskString(value string, stats *MaskStats) string {
	if value == "" {
		return value
	}
	for _, sp := range secretPatterns {
		value = maskByPattern(value, sp, stats)
	}
	return value
}

func maskByPattern(value string, sp secretPattern, stats *MaskStats) string {
	allIdxs := sp.re.FindAllStringSubmatchIndex(value, -1)
	if len(allIdxs) == 0 {
		return value
	}

	var sb strings.Builder
	cursor := 0

	for _, loc := range allIdxs {
		fullStart := loc[0]
		fullEnd := loc[1]

		type span struct{ start, end int }
		var spans []span
		for _, gi := range sp.secretGroupIdxs {
			gStart := loc[gi*2]
			gEnd := loc[gi*2+1]
			if gStart < 0 || gStart == gEnd {
				continue
			}
			spans = append(spans, span{gStart, gEnd})
			stats.MaskedSecrets++
		}
		if len(spans) == 0 {
			continue
		}
		stats.addRuleHit()

		sb.WriteString(value[cursor:fullStart])
		innerCursor := fullStart
		for _, s := range spans {
			sb.WriteString(value[innerCursor:s.start])
			sb.WriteString(maskValue)
			innerCursor = s.end
		}
		sb.WriteString(value[innerCursor:fullEnd])
		cursor = fullEnd
	}

	if cursor == 0 {
		return value
	}
	sb.WriteString(value[cursor:])
	return sb.String()
}

// ────────────────────────────────────────────────────────────────────────────
// Whitelist helpers
// ────────────────────────────────────────────────────────────────────────────

func isWhitelisted(wl map[string]struct{}, currentPath, key string) bool {
	if _, ok := wl[currentPath]; ok {
		return true
	}
	_, ok := wl[key]
	return ok
}

func appendPath(parent, key string) string {
	if parent == "" {
		return key
	}
	return parent + "." + key
}

// parseWhitelistFields parses either a JSON array or a comma-separated string
// into a set. Returns an empty (non-nil) map on empty/nil input.
func parseWhitelistFields(fields string) map[string]struct{} {
	result := make(map[string]struct{})
	s := strings.TrimSpace(fields)
	if s == "" {
		return result
	}

	if strings.HasPrefix(s, "[") && strings.HasSuffix(s, "]") {
		var arr []string
		if err := json.Unmarshal([]byte(s), &arr); err == nil {
			for _, item := range arr {
				item = strings.TrimSpace(item)
				if item != "" {
					result[item] = struct{}{}
				}
			}
			return result
		}
		logger.Warnf("string_desensitize: parse whitelist as JSON array failed, fallback to comma-split, input=%s", fields)
	}

	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part != "" {
			result[part] = struct{}{}
		}
	}
	return result
}
