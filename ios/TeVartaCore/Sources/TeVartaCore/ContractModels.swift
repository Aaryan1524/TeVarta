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
