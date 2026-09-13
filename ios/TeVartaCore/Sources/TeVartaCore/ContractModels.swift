import Foundation

// These types decode the exact shared JSON fixtures. Keep endpoint behavior in
// the OpenAPI contract; keep UI state and navigation outside this module.
public struct TVUser: Codable, Equatable, Sendable {
    public let id: String
    public let timezone: String
}

public struct AuthResponse: Codable, Equatable, Sendable {
    public let accessToken: String
    public let refreshToken: String
    public let tokenType: String
    public let expiresAt: String
    public let user: TVUser
}

public struct EntityCandidate: Codable, Equatable, Sendable {
    public let id: String
    public let name: String
    public let entityType: String
    public let ticker: String?
    public let lifecycle: String
}

public struct EntitySearchResponse: Codable, Equatable, Sendable {
    public let query: String
    public let candidates: [EntityCandidate]
    public let ambiguous: Bool
}

public struct Seed: Codable, Equatable, Sendable {
    public let entity: EntityCandidate
    public let weight: Double
    public let source: String
}

public struct SeedResponse: Codable, Equatable, Sendable {
    public let seed: Seed
    public let created: Bool
}

public struct SeedsStatusResponse: Codable, Equatable, Sendable {
    public let status: String
    public let updatedAt: String
    public let detail: String?
}

public struct FeedCard: Codable, Equatable, Identifiable, Sendable {
    public var id: String { eventId }
    public let eventId: String
    public let revision: Int
    public let category: String
    public let title: String
    public let why: String
    public let score: Int
    public let confidence: Double
    public let hops: Int
    public let importance: Double
    public let publishedAt: String
    public let articleCount: Int
    public let primarySource: String
    public let entities: [EntityCandidate]
    public let path: [String]
    public let slotType: String
    public let slotPosition: Int
}

public struct FeedResponse: Codable, Equatable, Sendable {
    public let items: [FeedCard]
    public let nextCursor: String?
}

public struct EventArticle: Codable, Equatable, Sendable {
    public let id: String
    public let publisher: String
    public let title: String
    public let url: String
    public let publishedAt: String
}

public struct EventDetailResponse: Codable, Equatable, Sendable {
    public let requestedEventId: String
    public let event: FeedCard
    public let articles: [EventArticle]
    public let mergedFrom: [String]
}

public struct GraphNode: Codable, Equatable, Sendable {
    public let id: String
    public let name: String
    public let entityType: String
}

public struct GraphEdge: Codable, Equatable, Sendable {
    public let sourceId: String
    public let targetId: String
    public let relation: String
    public let confidence: Double
}

public struct GraphResponse: Codable, Equatable, Sendable {
    public let nodes: [GraphNode]
    public let edges: [GraphEdge]
}

public struct EntityResponse: Codable, Equatable, Sendable {
    public let entity: EntityCandidate
    public let graphStatus: String
}

public struct Profile: Codable, Equatable, Sendable {
    public let timezone: String
    public let notificationPercentile: Int
    public let quietStart: String
    public let quietEnd: String
}

public struct ProfileResponse: Codable, Equatable, Sendable {
    public let profile: Profile
}

public struct DeviceResponse: Codable, Equatable, Sendable {
    public let id: String
    public let registered: Bool
}

public struct FeedbackResponse: Codable, Equatable, Sendable {
    public let accepted: Bool
    public let deduplicated: Bool
}

public enum FeedbackAction: String, CaseIterable, Codable, Sendable {
    case appOpen = "app_open"
    case feedView = "feed_view"
    case cardImpression = "card_impression"
    case cardClick = "card_click"
    case cardDismiss = "card_dismiss"
    case notRelevant = "not_relevant"
    case save
    case pushReceived = "push_received"
    case pushOpened = "push_opened"
    case digestOpened = "digest_opened"
    case seedAdded = "seed_added"
    case seedRemoved = "seed_removed"
    case sessionLength = "session_length"
}

public struct FeedbackRequest: Codable, Equatable, Sendable {
    public let eventId: String?
    public let action: FeedbackAction
    public let slotType: String?
    public let slotPosition: Int?
    public let sessionDurationS: Double?
    public let occurredAt: String
    public let idempotencyKey: String

    public init(eventId: String? = nil, action: FeedbackAction, slotType: String? = nil,
                slotPosition: Int? = nil, sessionDurationS: Double? = nil,
                occurredAt: String, idempotencyKey: String) {
        self.eventId = eventId
        self.action = action
        self.slotType = slotType
        self.slotPosition = slotPosition
        self.sessionDurationS = sessionDurationS
        self.occurredAt = occurredAt
        self.idempotencyKey = idempotencyKey
    }
}

public enum TelemetryClientError: Error, Equatable, Sendable {
    case missingAccessToken
    case invalidResponse
    case rejected(statusCode: Int, code: String?, message: String?)
}

public protocol FeedbackTransport: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, URLResponse)
}

public struct URLSessionFeedbackTransport: FeedbackTransport {
    private let session: URLSession

    public init(session: URLSession = .shared) {
        self.session = session
    }

    public func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        try await session.data(for: request)
    }
}

/// The app-facing interface for recording the §16 telemetry envelope.
///
/// The access token is supplied at send time so a refreshed session is used
/// without storing credentials in the telemetry client.
public struct TelemetryClient<Transport: FeedbackTransport>: Sendable {
    private let baseURL: URL
    private let accessToken: @Sendable () async -> String?
    private let transport: Transport

    public init(
        baseURL: URL,
        accessToken: @escaping @Sendable () async -> String?,
        transport: Transport
    ) {
        self.baseURL = baseURL
        self.accessToken = accessToken
        self.transport = transport
    }

    public func record(_ event: FeedbackRequest) async throws -> FeedbackResponse {
        guard let token = await accessToken(), !token.isEmpty else {
            throw TelemetryClientError.missingAccessToken
        }

        var request = URLRequest(url: baseURL.appending(path: "v1/feedback"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.httpBody = try ContractEncoder.json.encode(event)

        let (data, response) = try await transport.send(request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw TelemetryClientError.invalidResponse
        }
        guard (200 ..< 300).contains(httpResponse.statusCode) else {
            let apiError = try? ContractDecoder.json.decode(ErrorEnvelope.self, from: data)
            throw TelemetryClientError.rejected(
                statusCode: httpResponse.statusCode,
                code: apiError?.error.code,
                message: apiError?.error.message
            )
        }
        return try ContractDecoder.json.decode(FeedbackResponse.self, from: data)
    }
}

public extension TelemetryClient where Transport == URLSessionFeedbackTransport {
    init(baseURL: URL, accessToken: @escaping @Sendable () async -> String?) {
        self.init(
            baseURL: baseURL,
            accessToken: accessToken,
            transport: URLSessionFeedbackTransport()
        )
    }
}

public struct AccountDeletionResponse: Codable, Equatable, Sendable {
    public let status: String
}

public struct LoadingResponse: Codable, Equatable, Sendable {
    public let state: String
    public let retryAfterSeconds: Int
}

public struct APIError: Codable, Equatable, Sendable {
    public let code: String
    public let message: String
    public let requestId: String
    public let details: [String: String]?
}

public struct ErrorEnvelope: Codable, Equatable, Sendable {
    public let error: APIError
}

public enum ContractDecoder {
    public static let json: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()
}

public enum ContractEncoder {
    public static let json: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return encoder
    }()
}
