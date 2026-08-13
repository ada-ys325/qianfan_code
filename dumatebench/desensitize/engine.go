package redaction

import (
	"encoding/json"
	"strconv"
	"strings"
)

type Engine struct {
	rules []Rule
}

func NewEngine(rules []Rule) *Engine {
	cloned := append([]Rule(nil), rules...)
	return &Engine{rules: cloned}
}

func (e *Engine) Rules() []Rule {
	return append([]Rule(nil), e.rules...)
}

func (e *Engine) AuditLine(line []byte) AuditStats {
	return e.AuditRecord(line).Stats
}

func (e *Engine) AuditRecord(line []byte) AuditResult {
	stats := NewAuditStats(e.rules)

	var value interface{}
	if err := json.Unmarshal(line, &value); err != nil {
		events := e.matchText(string(line), ScopeText|ScopeField, "$raw")
		stats.AddRecord(countMatches(events), false)
		return AuditResult{Stats: stats, Matches: events}
	}

	events := e.walkValue(value, "")
	stats.AddRecord(countMatches(events), true)
	return AuditResult{Stats: stats, Matches: events}
}

func (e *Engine) walkValue(value interface{}, path string) []MatchEvent {
	var events []MatchEvent
	switch typed := value.(type) {
	case map[string]interface{}:
		for key, child := range typed {
			events = append(events, e.walkValue(child, appendFieldPath(path, key))...)
		}
	case []interface{}:
		for index, child := range typed {
			events = append(events, e.walkValue(child, appendArrayPath(path, index))...)
		}
	case string:
		if nested, ok := parseNestedJSON(typed); ok {
			return e.walkValue(nested, path)
		}
		textEvents := e.matchText(typed, ScopeText, path)
		events = append(events, textEvents...)
		if path != "" {
			events = append(events, e.matchField(typed, path, textEvents)...)
		}
	}
	return events
}

func (e *Engine) matchField(value, path string, textEvents []MatchEvent) []MatchEvent {
	matchedInText := make(map[string]struct{}, len(textEvents))
	for _, event := range textEvents {
		matchedInText[event.RuleID] = struct{}{}
	}
	canonical := fieldName(path) + `="` + value + `"`
	var events []MatchEvent
	for _, rule := range e.rules {
		if rule.Scope&ScopeField == 0 {
			continue
		}
		if _, ok := matchedInText[rule.ID]; ok {
			continue
		}
		matches := matchRule(canonical, rule, path)
		for range matches {
			events = append(events, MatchEvent{
				RuleID:    rule.ID,
				FieldPath: path,
				Text:      value,
				Start:     0,
				End:       len(value),
			})
		}
	}
	return events
}

func (e *Engine) matchText(text string, scope Scope, path string) []MatchEvent {
	var events []MatchEvent
	for _, rule := range e.rules {
		if rule.Scope&scope == 0 {
			continue
		}
		events = append(events, matchRule(text, rule, path)...)
	}
	return events
}

func matchRule(text string, rule Rule, path string) []MatchEvent {
	indexes := rule.Pattern.FindAllStringSubmatchIndex(text, -1)
	events := make([]MatchEvent, 0, len(indexes))
	for _, index := range indexes {
		start, end, ok := matchRange(index, rule.ValueGroup)
		if !ok {
			continue
		}
		value := text[start:end]
		if rule.Validator != nil && !rule.Validator(value) {
			continue
		}
		events = append(events, MatchEvent{
			RuleID:    rule.ID,
			FieldPath: path,
			Text:      text,
			Start:     start,
			End:       end,
		})
	}
	return events
}

func countMatches(events []MatchEvent) map[string]int64 {
	counts := make(map[string]int64)
	for _, event := range events {
		counts[event.RuleID]++
	}
	return counts
}

func matchRange(index []int, group int) (int, int, bool) {
	position := group * 2
	if position < 0 || position+1 >= len(index) {
		return 0, 0, false
	}
	start, end := index[position], index[position+1]
	return start, end, start >= 0 && end > start
}

func parseNestedJSON(value string) (interface{}, bool) {
	trimmed := strings.TrimSpace(value)
	if len(trimmed) < 2 {
		return nil, false
	}
	if (trimmed[0] != '{' || trimmed[len(trimmed)-1] != '}') &&
		(trimmed[0] != '[' || trimmed[len(trimmed)-1] != ']') {
		return nil, false
	}
	var nested interface{}
	if err := json.Unmarshal([]byte(trimmed), &nested); err != nil {
		return nil, false
	}
	return nested, true
}

func appendFieldPath(parent, key string) string {
	if parent == "" {
		return key
	}
	return parent + "." + key
}

func appendArrayPath(parent string, index int) string {
	return parent + "[" + strconv.Itoa(index) + "]"
}

func fieldName(path string) string {
	if index := strings.LastIndex(path, "."); index >= 0 {
		path = path[index+1:]
	}
	if index := strings.Index(path, "["); index >= 0 {
		path = path[:index]
	}
	return path
}
