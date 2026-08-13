package ruleset

import (
	"fmt"
	"regexp"
	"strings"
	"time"

	"strategy-data-acquisition/worker/executor/sensitive_redaction_audit/redaction"
)

const Version = "dlp_v1"

var allRules = []redaction.Rule{
	{
		ID:          "ID_CARD",
		Description: "中国大陆身份证 18 位",
		Status:      "active",
		Pattern:     regexp.MustCompile(`(^|[^0-9])([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])([^0-9A-Za-z]|$)`),
		ValueGroup:  2,
		Scope:       redaction.ScopeText,
		Validator:   validIDCardDate,
	},
	{
		ID:          "EMAIL",
		Description: "邮箱地址",
		Status:      "active",
		Pattern:     regexp.MustCompile(`[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}`),
		Scope:       redaction.ScopeText,
	},
	{
		ID:          "PHONE_CN",
		Description: "中国大陆手机号",
		Status:      "active",
		Pattern:     regexp.MustCompile(`(^|[^0-9])(1[3-9]\d{9})([^0-9]|$)`),
		ValueGroup:  2,
		Scope:       redaction.ScopeText,
	},
	{
		ID:          "PLATE_CN",
		Description: "中国车牌",
		Status:      "active",
		Pattern:     regexp.MustCompile(`[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领][A-Z][A-HJ-NP-Z0-9]{4,5}[A-HJ-NP-Z0-9挂学警港澳]`),
		Scope:       redaction.ScopeText,
	},
	{
		ID:          "PRIVATE_KEY",
		Description: "私钥块",
		Status:      "candidate",
		Pattern:     regexp.MustCompile(`(?s)-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----.*?-----END (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----`),
		Scope:       redaction.ScopeText,
	},
	{
		ID:          "DB_CONN",
		Description: "数据库连接串",
		Status:      "candidate",
		Pattern:     regexp.MustCompile(`(?i)\b(?:mysql|postgres(?:ql)?|mongodb(?:\+srv)?|redis|amqp|oracle)://[^\s'"<>]+`),
		Scope:       redaction.ScopeText,
	},
	{
		ID:          "JWT",
		Description: "JSON Web Token",
		Status:      "candidate",
		Pattern:     regexp.MustCompile(`\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b`),
		Scope:       redaction.ScopeText,
	},
	{
		ID:          "GITHUB_TOKEN",
		Description: "GitHub Personal Access Token",
		Status:      "candidate",
		Pattern:     regexp.MustCompile(`\bgh[pousr]_[A-Za-z0-9]{36,255}\b`),
		Scope:       redaction.ScopeText,
	},
	{
		ID:          "SECRET_KV_QUOTED",
		Description: "带引号的凭据赋值",
		Status:      "candidate",
		Pattern:     regexp.MustCompile(`(?i)['"]?(?:api[*\-]?key|access[*\-]?(?:token|key)|secret(?:[*\-]?key)?|auth[*\-]?token|client[_\-]?secret|password|passwd|pwd|密码|口令|密钥|令牌)['"]?\s*[:=]\s*['"]([^'"]{4,})['"]`),
		ValueGroup:  1,
		Scope:       redaction.ScopeText | redaction.ScopeField,
	},
	{
		ID:          "PASSWORD_FUNC",
		Description: "函数第一参数位置的明文口令",
		Status:      "candidate",
		Pattern:     regexp.MustCompile(`(?i)(?P<name>\b[-.\w]*(?:set)?[-.*]?(?:pass[0-3]?|pwd[0-3]?|password[0-3]?|passwd[0-3]?)*?(?:test|dev|prod|off|online)?)\(['"](?P<sensitive>[\w+\-/$*@^!():?=,#]{6,50})['"]\)`),
		ValueGroup:  2,
		Scope:       redaction.ScopeText,
	},
}

func Rules(enabled []string) ([]redaction.Rule, error) {
	if len(enabled) == 0 {
		return append([]redaction.Rule(nil), allRules...), nil
	}
	wanted := make(map[string]struct{}, len(enabled))
	for _, ruleID := range enabled {
		ruleID = strings.ToUpper(strings.TrimSpace(ruleID))
		if ruleID != "" {
			wanted[ruleID] = struct{}{}
		}
	}
	rules := make([]redaction.Rule, 0, len(wanted))
	for _, rule := range allRules {
		if _, ok := wanted[rule.ID]; ok {
			rules = append(rules, rule)
			delete(wanted, rule.ID)
		}
	}
	if len(wanted) > 0 {
		unknown := make([]string, 0, len(wanted))
		for ruleID := range wanted {
			unknown = append(unknown, ruleID)
		}
		return nil, fmt.Errorf("unknown sensitive redaction audit rules: %s", strings.Join(unknown, ","))
	}
	return rules, nil
}

func validIDCardDate(value string) bool {
	if len(value) != 18 {
		return false
	}
	_, err := time.Parse("20060102", value[6:14])
	return err == nil
}
