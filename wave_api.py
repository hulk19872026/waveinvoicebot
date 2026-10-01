"""Thin client for Wave's public GraphQL API (https://gql.waveapps.com/graphql/public)."""
import requests

API_URL = "https://gql.waveapps.com/graphql/public"


class WaveError(Exception):
    pass


CUSTOMER_FIELDS = ("id name email phone mobile "
                   "address { addressLine1 addressLine2 city postalCode province { code name } country { code } }")
COUNTRY_ALIASES = {"USA": "US", "U.S.": "US", "U.S.A.": "US", "UNITED STATES": "US", "CANADA": "CA"}


def format_address(a):
    """One-line address from a Wave Address object (or None if empty)."""
    if not a:
        return None
    prov = (a.get("province") or {}).get("code") or ""
    region = " ".join(x for x in (prov.split("-")[-1], a.get("postalCode")) if x)
    parts = [a.get("addressLine1"), a.get("addressLine2"), a.get("city"), region]
    return ", ".join(x for x in parts if x) or None


def _with_address(c):
    c["address_text"] = format_address(c.get("address"))
    return c


class Wave:
    def __init__(self, token, business_id=None, income_account_id=None):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        self.business_id = business_id or self._first_business_id()
        self.income_account_id = income_account_id or self._first_income_account_id()
        self.customers, self.products = [], []
        self._provinces = {}
        self.refresh()

    # ---------- low level ----------
    def gql(self, query, variables=None):
        r = self.s.post(API_URL, json={"query": query, "variables": variables or {}}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise WaveError("; ".join(e.get("message", "?") for e in data["errors"]))
        return data["data"]

    def _mutate(self, name, input_type, input_obj, returning):
        q = f"""mutation($input: {input_type}!) {{
            {name}(input: $input) {{
                didSucceed
                inputErrors {{ message path }}
                {returning}
            }}
        }}"""
        out = self.gql(q, {"input": input_obj})[name]
        if not out["didSucceed"]:
            errs = "; ".join(f"{'.'.join(e.get('path') or [])}: {e['message']}" for e in out["inputErrors"] or [])
            raise WaveError(f"{name} failed: {errs or 'unknown error'}")
        return out

    def _paged(self, field, fields, extra_args=""):
        out, page = [], 1
        while True:
            q = f"""query($b: ID!, $p: Int!) {{
                business(id: $b) {{
                    {field}(page: $p, pageSize: 100{extra_args}) {{
                        pageInfo {{ totalPages }}
                        edges {{ node {{ {fields} }} }}
                    }}
                }}
            }}"""
            d = self.gql(q, {"b": self.business_id, "p": page})["business"][field]
            out += [e["node"] for e in d["edges"]]
            if page >= (d["pageInfo"]["totalPages"] or 1):
                return out
            page += 1

    # ---------- setup ----------
    def _first_business_id(self):
        d = self.gql("query { businesses(page: 1, pageSize: 20) { edges { node { id name isPersonal } } } }")
        biz = [e["node"] for e in d["businesses"]["edges"] if not e["node"]["isPersonal"]]
        if not biz:
            raise WaveError("No business found on this Wave account.")
        return biz[0]["id"]

    def _first_income_account_id(self):
        accts = self._paged("accounts", "id name isArchived", ", subtypes: [INCOME]")
        accts = [a for a in accts if not a["isArchived"]]
        return accts[0]["id"] if accts else None

    def refresh(self):
        """Reload clients and products from Wave."""
        self.customers = [_with_address(c) for c in self._paged("customers", CUSTOMER_FIELDS)]
        prods = self._paged("products", "id name description unitPrice isSold isArchived defaultSalesTaxes { id }")
        self.products = [p for p in prods if p["isSold"] and not p["isArchived"]]

    # ---------- create ----------
    def _province_code(self, country, state):
        """Wave's code for a state/province (e.g. "NY" -> Wave's NY code), looked up once per country."""
        if not state:
            return None
        if country not in self._provinces:
            q = "query($c: CountryCode!) { country(code: $c) { provinces { code name } } }"
            self._provinces[country] = (self.gql(q, {"c": country})["country"] or {}).get("provinces") or []
        st = state.strip().upper()
        for p in self._provinces[country]:
            code = p["code"].upper()
            if st in (code, code.split("-")[-1], p["name"].upper()):
                return p["code"]
        return None

    def _address_input(self, a):
        country = (a.get("country") or "US").strip().upper()
        country = COUNTRY_ALIASES.get(country, country if len(country) == 2 else "US")
        inp = {"addressLine1": a.get("line1"), "addressLine2": a.get("line2"), "city": a.get("city"),
               "postalCode": a.get("zip"), "countryCode": country,
               "provinceCode": self._province_code(country, a.get("state"))}
        return {k: v for k, v in inp.items() if v}

    def create_customer(self, name, email=None, phone=None, first_name=None, last_name=None, address=None):
        inp = {"businessId": self.business_id, "name": name}
        for k, v in {"email": email, "phone": phone, "firstName": first_name, "lastName": last_name}.items():
            if v:
                inp[k] = v
        if address:
            inp["address"] = self._address_input(address)
        c = self._mutate("customerCreate", "CustomerCreateInput", inp, f"customer {{ {CUSTOMER_FIELDS} }}")["customer"]
        self.customers.append(_with_address(c))
        return c

    def update_customer(self, customer_id, **changes):
        """Change a client's name/email/phone/address in Wave. Only fields given are changed."""
        address = changes.pop("address", None)
        inp = {"id": customer_id, **{k: v for k, v in changes.items() if v}}
        if address:
            inp["address"] = self._address_input(address)
        c = _with_address(self._mutate("customerPatch", "CustomerPatchInput", inp,
                                       f"customer {{ {CUSTOMER_FIELDS} }}")["customer"])
        self.customers = [c if x["id"] == c["id"] else x for x in self.customers]
        return c

    def create_product(self, name, unit_price, description=None):
        if not self.income_account_id:
            raise WaveError("No income account found; set WAVE_INCOME_ACCOUNT_ID.")
        inp = {
            "businessId": self.business_id,
            "name": name,
            "unitPrice": str(unit_price),
            "incomeAccountId": self.income_account_id,
        }
        if description:
            inp["description"] = description
        p = self._mutate("productCreate", "ProductCreateInput", inp,
                         "product { id name description unitPrice isSold isArchived defaultSalesTaxes { id } }")["product"]
        self.products.append(p)
        return p

    @staticmethod
    def _items(items):
        out = []
        for it in items:
            row = {
                "productId": it["product_id"],
                "quantity": str(it["quantity"]),
                "unitPrice": str(it["unit_price"]),
            }
            if it.get("description"):
                row["description"] = it["description"]
            if it.get("tax_ids"):
                row["taxes"] = [{"salesTaxId": t} for t in it["tax_ids"]]
            out.append(row)
        return out

    # ---------- invoices & estimates ----------
    @staticmethod
    def _num_field(kind):
        return "estimateNumber" if kind == "estimate" else "invoiceNumber"

    def _doc_fields(self, kind):
        return f"id {self._num_field(kind)} status viewUrl pdfUrl total {{ value }} customer {{ {CUSTOMER_FIELDS} }}"

    def _doc(self, kind, d):
        """Normalize a Wave invoice/estimate into a plain dict."""
        return {"kind": kind, "id": d["id"], "number": d[self._num_field(kind)], "status": d["status"],
                "view_url": d["viewUrl"], "pdf_url": d["pdfUrl"], "total": d["total"]["value"],
                "client": d["customer"]["name"], "email": d["customer"].get("email"),
                "phone": d["customer"].get("phone") or d["customer"].get("mobile"),
                "address": format_address(d["customer"].get("address"))}

    def create_invoice(self, customer_id, items, memo=None):
        inp = {"businessId": self.business_id, "customerId": customer_id,
               "status": "DRAFT", "items": self._items(items)}
        if memo:
            inp["memo"] = memo
        out = self._mutate("invoiceCreate", "InvoiceCreateInput", inp, f"invoice {{ {self._doc_fields('invoice')} }}")
        return self._doc("invoice", out["invoice"])

    def create_estimate(self, customer_id, items, memo=None):
        inp = {"businessId": self.business_id, "customerId": customer_id, "items": self._items(items)}
        if memo:
            inp["memo"] = memo
        out = self._mutate("estimateCreate", "EstimateCreateInput", inp, f"estimate {{ {self._doc_fields('estimate')} }}")
        return self._doc("estimate", out["estimate"])

    def get_doc(self, kind, doc_id):
        q = f"""query($b: ID!, $id: ID!) {{
            business(id: $b) {{ {kind}(id: $id) {{ {self._doc_fields(kind)} }} }}
        }}"""
        d = self.gql(q, {"b": self.business_id, "id": doc_id})["business"][kind]
        return self._doc(kind, d) if d else None

    def find_doc(self, kind, number):
        """Look up an invoice/estimate by its number (as shown in Wave)."""
        sort = "ESTIMATE_NUMBER_DESC" if kind == "estimate" else "[INVOICE_NUMBER_DESC]"
        q = f"""query($b: ID!, $n: String!) {{
            business(id: $b) {{
                {kind}s(page: 1, pageSize: 1, sort: {sort}, {self._num_field(kind)}: $n) {{
                    edges {{ node {{ {self._doc_fields(kind)} }} }}
                }}
            }}
        }}"""
        edges = self.gql(q, {"b": self.business_id, "n": str(number)})["business"][f"{kind}s"]["edges"]
        return self._doc(kind, edges[0]["node"]) if edges else None

    def _approve_if_draft(self, doc):
        # Wave won't email or convert a draft; approving just finalizes it (nothing is sent).
        if doc["status"] == "DRAFT":
            k = doc["kind"]
            self._mutate(f"{k}Approve", f"{k.title()}ApproveInput", {f"{k}Id": doc["id"]}, f"{k} {{ id }}")

    def send_doc(self, doc, to):
        """Email an invoice/estimate (with PDF) to the client. Requires email sending enabled in Wave."""
        self._approve_if_draft(doc)
        k = doc["kind"]
        self._mutate(f"{k}Send", f"{k.title()}SendInput",
                     {f"{k}Id": doc["id"], "to": [to], "attachPDF": True}, f"{k} {{ id }}")

    def convert_estimate(self, doc):
        """Turn an estimate into an invoice. Returns the new invoice."""
        self._approve_if_draft(doc)
        out = self._mutate("convertEstimateToInvoice", "ConvertEstimateToInvoiceInput",
                           {"estimateId": doc["id"]}, "invoiceId")
        return self.get_doc("invoice", out["invoiceId"])
